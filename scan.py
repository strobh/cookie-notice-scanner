#!/usr/bin/env python3

import argparse
import base64
import json
import multiprocessing as mp
import os
import subprocess
import traceback
from functools import partial
from multiprocessing import Lock
from urllib.parse import urlparse

import pychrome
import tld.exceptions
from abp.filters import parse_filterlist
from abp.filters.parser import Filter
from langdetect import detect
from pprint import pprint
from tld import get_fld, get_tld
from tranco import Tranco


# Possible improvements:
# - if cookie notice is displayed in iframe (e.g. forbes.com), currently the 
# iframe is assumed to be the cookie notice; one might even walk up the tree 
# even further and find a fixed or full-width parent there


FAILED_REASON_TIMEOUT = 'Page.navigate timeout'
FAILED_REASON_STATUS_CODE = 'status code'
FAILED_REASON_LOADING = 'loading failed'


class Webpage:
    def __init__(self, rank=None, domain='', protocol='https'):
        self.rank = rank
        self.domain = domain
        self.protocol = protocol
        self.url = f'{self.protocol}://{self.domain}'

    def set_protocol(self, protocol):
        self.protocol = protocol
        self.url = f'{self.protocol}://{self.domain}'

    def set_subdomain(self, subdomain):
        self.url = f'{self.protocol}://{subdomain}.{self.domain}'

    def remove_subdomain(self):
        self.url = f'{self.protocol}://{self.domain}'


class WebpageResult:
    def __init__(self, webpage):
        self.rank = webpage.rank
        self.domain = webpage.domain
        self.tld = get_tld(webpage.url)
        self.protocol = webpage.protocol
        self.url = webpage.url

        self.redirects = []

        self.failed = False
        self.failed_reason = None
        self.failed_exception = None
        self.failed_traceback = None

        self.warnings = []

        self.stopped_waiting = False
        self.stopped_waiting_reason = None

        self.requests = []
        self.responses = []
        self.cookies = {}
        self.screenshots = {}

        self.html = None
        self.language = None
        self.is_cmp_defined = False
        self.cookie_notice_count = {}
        self.cookie_notices = {}

        self._json_excluded_fields = ['_json_excluded_fields', 'screenshots']

    def add_redirect(self, url, root_frame=True):
        self.redirects.append({
                'url': url,
                'root_frame': root_frame
            })

    def set_failed(self, reason, exception=None, traceback=None):
        self.failed = True
        self.failed_reason = reason
        self.failed_exception = exception
        self.failed_traceback = traceback

    def add_warning(self, warning):
        self.warnings.append(warning)

    def set_stopped_waiting(self, reason):
        self.stopped_waiting = True
        self.stopped_waiting_reason = reason

    def add_request(self, request_url):
        self.requests.append({
            'url': request_url,
        })

    def add_response(self, requested_url, status, mime_type, headers):
        self.responses.append({
            'url': requested_url,
            'status': status,
            'mime_type': mime_type,
            'headers': headers,
        })

    def set_cookies(self, key, cookies):
        self.cookies[key] = cookies

    def add_screenshot(self, name, screenshot):
        self.screenshots[name] = screenshot

    def set_html(self, html):
        self.html = html

    def set_language(self, language):
        self.language = language

    def set_cmp_defined(self, is_cmp_defined):
        self.is_cmp_defined = is_cmp_defined

    def add_cookie_notices(self, detection_technique, cookie_notices):
        self.cookie_notice_count[detection_technique] = len(cookie_notices)
        self.cookie_notices[detection_technique] = cookie_notices

    def save_screenshots(self, directory):
        for name, screenshot in self.screenshots.items():
            self._save_screenshot(name, screenshot, directory)

    def _save_screenshot(self, name, screenshot, directory):
        with open(f'{directory}/{self._get_filename_for_screenshot(name)}', 'wb') as file:
            file.write(base64.b64decode(screenshot))

    def _get_filename_for_screenshot(self, name):
        return f'{self.rank}-{self.domain}-{name}.png'

    def save_data(self, directory):
        with open(f'{directory}/{self._get_filename_for_data()}', 'w', encoding='utf8') as file:
            file.write(self._to_json())

    def _get_filename_for_data(self):
        return f'{self.rank}-{self.domain}.json'

    def _to_json(self):
        results = {k: v for k, v in self.__dict__.items() if k not in self._json_excluded_fields}
        return json.dumps(results, indent=4, default=lambda o: o.__dict__, ensure_ascii=False)

    def exclude_field_from_json(self, excluded_field):
        self._json_excluded_fields.append(excluded_field)


class Click:
    def __init__(self, detection_technique, cookie_notice_index, clickable_index):
        self.detection_technique = detection_technique
        self.cookie_notice_index = cookie_notice_index
        self.clickable_index = clickable_index


class ClickResult:
    def __init__(self):
        self.cookies = {}
        self.new_pages = []
        self.cookie_notice_visible_after_click = None
        self.is_page_modal = None

    def set_cookies(self, key, cookies):
        self.cookies[key] = cookies

    def add_new_page(self, url, root_frame=True, new_window=False):
        new_page = {
                'url': url,
                'root_frame': root_frame,
                'new_window': new_window,
            }
        if new_page not in self.new_pages:
            self.new_pages.append(new_page)

    def has_new_pages(self):
        return len(self.new_pages) > 0

    def set_cookie_notice_visible_after_click(self, visible_after_click):
        self.cookie_notice_visible_after_click = visible_after_click

    def set_is_page_modal(self, is_page_modal):
        self.is_page_modal = is_page_modal


class Browser:
    def __init__(self, abp_filter_filenames, debugger_url='http://127.0.0.1:9222'):
        # create a browser instance which controls chromium
        self.browser = pychrome.Browser(url=debugger_url)

        # create helpers
        self.abp_filters = {
                os.path.splitext(os.path.basename(abp_filter_filename))[0]: AdblockPlusFilter(abp_filter_filename) 
                for abp_filter_filename in abp_filter_filenames
            }

    def scan_page(self, webpage, do_click=False):
        """Tries to scan the webpage and returns the result of the scan.

        Following possibilities are tried to scan the page:
        - https protocol without `www.` subdomain
        - https protocol with `www.` subdomain
        - http protocol without `www.` subdomain
        - http protocol with `www.` subdomain

        The first scan whose result is not failed is returned.
        """
        result = self._scan_page(webpage).get_result()

        # try https with subdomain www
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.set_subdomain('www')
            result = self._scan_page(webpage).get_result()
        # try http without subdomain www
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.remove_subdomain()
            webpage.set_protocol('http')
            result = self._scan_page(webpage).get_result()
        # try http with www subdomain
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.set_subdomain('www')
            result = self._scan_page(webpage).get_result()

        if result.failed:
            return result

        # do the click and add the click results to the web page result
        if do_click:
            self.do_click(webpage, result)
        return result

    def do_click(self, webpage, result):
        # store click results for nodes to avoid duplicates
        click_results = {}

        # click each element and add the click result to the webpage result
        for detection_technique, cookie_notices in result.cookie_notices.items():
            for cookie_notice_index, cookie_notice in enumerate(cookie_notices):
                for clickable_index, clickable in enumerate(cookie_notice.get('clickables')):
                    # check whether click was already done
                    if clickable.get('node_id') in click_results:
                        clickable['click_result'] = click_results.get(clickable.get('node_id'))
                    else:
                        # create click instruction
                        click = Click(detection_technique, cookie_notice_index, clickable_index)
                        # do click on web page
                        click_result = self._scan_page(webpage=webpage, take_screenshots=False, click=click).get_click_result()
                        # store results
                        clickable['click_result'] = click_result
                        click_results[clickable.get('node_id')] = click_result

    def _scan_page(self, webpage, take_screenshots=True, click=None):
        """Creates tab, scans webpage and returns result."""
        tab = self.browser.new_tab()

        # scan the page
        page_scanner = WebpageScanner(tab=tab, abp_filters=self.abp_filters, webpage=webpage)
        page_scanner.scan(take_screenshots=take_screenshots, click=click)

        # close tab and obtain the results
        self.browser.close_tab(tab)
        return page_scanner


class AdblockPlusFilter:
    def __init__(self, rules_filename):
        with open(rules_filename) as filterlist:
            # we only need filters with type css
            # other instances are Header, Metadata, etc.
            # other type is url-pattern which is used to block script files
            self._rules = [rule for rule in parse_filterlist(filterlist) if isinstance(rule, Filter) and rule.selector.get('type') == 'css']

    def get_applicable_rules(self, domain):
        """Returns the rules of the filter that are applicable for the given domain."""
        return [rule for rule in self._rules if self._is_rule_applicable(rule, domain)]

    def _is_rule_applicable(self, rule, domain):
        """Tests whethere a given rule is applicable for the given domain."""
        domain_options = [(key, value) for key, value in rule.options if key == 'domain']
        if len(domain_options) == 0:
            return True

        # there is only one domain option
        _, domains = domain_options[0]

        # filter exclusion rules as they should be ignored:
        # the cookie notices do exist, the ABP plugin is just not able
        # to remove them correctly
        domains = [(opt_domain, opt_applicable) for opt_domain, opt_applicable in domains if opt_applicable == True]
        if len(domains) == 0:
            return True

        # the list of domains now only consists of domains for which the rule
        # is applicable, we check for the domain and return False otherwise
        for opt_domain, _ in domains:
            if opt_domain in domain:
                return True
        return False


class WebpageScanner:
    def __init__(self, tab, abp_filters, webpage):
        self.tab = tab
        self.abp_filters = abp_filters
        self.webpage = webpage
        self.result = WebpageResult(webpage)
        self.click_result = ClickResult()
        self.loaded_urls = []

    def scan(self, take_screenshots=True, click=None):
        self._setup()
        
        try:
            # open url and wait for load event and js
            self._navigate_and_wait()
            if self.result.failed:
                return self.result

            # get root node of document, is needed to be sure that the DOM is loaded
            self.root_node = self.tab.DOM.getDocument().get('root')

            # store html of page
            self.result.set_html(self._get_html_of_node(self.root_node.get('nodeId')))

            # detect language and cookie notices
            self.detect_language()
            self.detect_cookie_notices(take_screenshots=take_screenshots)

            # get all cookies
            self.result.set_cookies('all', self._get_all_cookies())

            # do the click if necessary
            self.do_click(click)
        except Exception as e:
            self.result.set_failed(str(e), type(e).__name__, traceback.format_exc())

        # stop the browser from executing javascript
        self.tab.Emulation.setScriptExecutionDisabled(value=True)
        self.tab.wait(0.1)

        try:
            # clear the browser
            self._clear_browser()
            self.tab.wait(0.1)
        except Exception as e:
            print(type(e).__name__)
            print(traceback.format_exc())
            print(f'clearing browser failed ({self.webpage.url})')

        # stop the tab
        self.tab.stop()

    def get_result(self):
        return self.result

    def get_click_result(self):
        return self.click_result


    ############################################################################
    # SETUP
    ############################################################################

    def _setup(self):
        # initialize `_is_loaded` variable to `False`
        # it will be set to `True` when the `loadEventFired` event occurs
        self._is_loaded = False

        # data about requests/repsonses
        self.recordRedirects = True
        self.recordNewPagesForClick = False
        self.waitForNavigatedEvent = False
        self.requestId = None
        self.frameId = None

        # setup the tab
        self._setup_tab()
        self.tab.wait(0.1)

        # deny permissions because they might pop-up and block detection
        #self._deny_permissions() # problems with ubuntu

    def _setup_tab(self):
        # set callbacks for request and response logging
        self.tab.Network.requestWillBeSent = self._event_request_will_be_sent
        self.tab.Network.responseReceived = self._event_response_received
        self.tab.Network.loadingFailed = self._event_loading_failed
        self.tab.Page.loadEventFired = self._event_load_event_fired
        self.tab.Page.frameRequestedNavigation = self._event_frame_requested_navigation
        self.tab.Page.frameStartedLoading = self._event_frame_started_loading
        self.tab.Page.navigatedWithinDocument = self._event_navigated_within_document
        self.tab.Page.windowOpen = self._event_window_open
        self.tab.Page.javascriptDialogOpening = self._event_javascript_dialog_opening
        
        # start our tab after callbacks have been registered
        self.tab.start()
        
        # enable network notifications for all request/response so our
        # callbacks actually receive some data
        self.tab.Network.enable()

        # enable page domain notifications so our load_event_fired
        # callback is called when the page is loaded
        self.tab.Page.enable()

        # enable DOM, Runtime and Overlay
        self.tab.DOM.enable()
        self.tab.Runtime.enable()
        self.tab.Overlay.enable()

    def _navigate_and_wait(self):
        try:
            # open url
            self._clear_browser()
            #self.tab.Page.bringToFront()
            self.tab.Page.navigate(url=self.webpage.url, _timeout=15)

            # return if failed to load page
            if self.result.failed:
                return

            # we wait for load event and JavaScript
            self._wait_for_load_event_and_js()
        except pychrome.exceptions.TimeoutException as e:
            self.result.set_failed(FAILED_REASON_TIMEOUT, type(e).__name__)

    def _clear_browser(self):
        """Clears cache, cookies, local storage, etc. of the browser."""
        self.tab.Network.clearBrowserCache()
        self.tab.Network.clearBrowserCookies()

        # store all domains that were requested
        first_level_domains = set()
        for loaded_url in self.loaded_urls:
            # invalid urls raise an exception
            try:
                first_level_domain = get_fld(loaded_url)
                first_level_domains.add(first_level_domain)
            except Exception:
                pass

        # clear the data for each domain
        for first_level_domain in first_level_domains:
            self.tab.Storage.clearDataForOrigin(origin='.' + first_level_domain, storageTypes='all')

    def _deny_permissions(self):
        self._deny_permission('notifications')
        self._deny_permission('geolocation')
        self._deny_permission('camera')
        self._deny_permission('microphone')

    def _deny_permission(self, permission):
        self._set_permission(permission, 'denied')

    def _set_permission(self, permission, value):
        permission_descriptor = {'name': permission}
        self.tab.Browser.setPermission(permission=permission_descriptor, setting=value)

    def _wait_for_load_event_and_js(self, load_event_timeout=30, js_timeout=5):
        self._wait_for_load_event(load_event_timeout)

        # wait for JavaScript code to be run, after the page has been loaded
        self.tab.wait(js_timeout)

    def _wait_for_load_event(self, load_event_timeout):
        # we wait for the load event to be fired (see `_event_load_event_fired`)
        waited = 0
        while not self._is_loaded and waited < load_event_timeout:
            self.tab.wait(0.1)
            waited += 0.1

        if waited >= load_event_timeout:
            self.result.set_stopped_waiting('load event')
            self.tab.Page.stopLoading()


    ############################################################################
    # EVENTS
    ############################################################################

    def _event_request_will_be_sent(self, request, requestId, **kwargs):
        """Will be called when a request is about to be sent.

        Those requests can still be blocked or intercepted and modified.
        This example script does not use any blocking or intercepting.

        Note: It does not say anything about the request being successful,
        there can still be connection issues.
        """
        url = request['url']
        self.result.add_request(request_url=url)

        # the request id of the first request is stored to be able to detect failures
        if self.requestId == None:
            self.requestId = requestId

        if self.frameId == None:
            self.frameId = kwargs.get('frameId', False)

    def _event_response_received(self, response, requestId, **kwargs):
        """Will be called when a response is received.

        This includes the originating request which resulted in the
        response being received.
        """
        self.loaded_urls.append(response['url'])

        url = response['url']
        mime_type = response['mimeType']
        status = response['status']
        headers = response['headers']
        self.result.add_response(requested_url=url, status=status, mime_type=mime_type, headers=headers)

        if requestId == self.requestId and (str(status).startswith('4') or str(status).startswith('5')):
            self.result.set_failed(FAILED_REASON_STATUS_CODE, str(status))

    def _event_loading_failed(self, requestId, errorText, **kwargs):
        if requestId == self.requestId:
            self.result.set_failed(FAILED_REASON_LOADING, errorText)

    def _event_frame_started_loading(self, frameId, **kwargs):
        if self.recordNewPagesForClick and frameId == self.frameId:
            self._is_loaded = False
            self.waitForNavigatedEvent = True

    def _event_frame_requested_navigation(self, url, frameId, **kwargs):
        is_root_frame = (self.frameId == frameId)
        if self.recordNewPagesForClick:
            self.click_result.add_new_page(url, root_frame=is_root_frame)

    def _event_navigated_within_document(self, url, frameId, **kwargs):
        is_root_frame = (self.frameId == frameId)
        if self.recordRedirects:
            self.result.add_redirect(url, root_frame=is_root_frame)
        if self.recordNewPagesForClick:
            self.click_result.add_new_page(url, root_frame=is_root_frame)

    def _event_window_open(self, url, **kwargs):
        if self.recordNewPagesForClick:
            self.click_result.add_new_page(url, new_window=True)

    def _event_load_event_fired(self, timestamp, **kwargs):
        """Will be called when the page sends an load event.

        Note that this only means that all resources are loaded, the
        page may still process some JavaScript.
        """
        self._is_loaded = True
        self.recordRedirects = False

    def _event_javascript_dialog_opening(self, message, type, **kwargs):
        if type == 'alert':
            self.tab.Page.handleJavaScriptDialog(accept=True)
        else:
            self.tab.Page.handleJavaScriptDialog(accept=False)


    ############################################################################
    # RESULT FOR CLICK ON ELEMENT
    ############################################################################

    def do_click(self, click):
        if not click:
            return

        # get cookies
        self.click_result.set_cookies('before_click', self._get_all_cookies())

        cookie_notices = self.result.cookie_notices.get(click.detection_technique, [])
        if len(cookie_notices) > click.cookie_notice_index:
            cookie_notice = cookie_notices[click.cookie_notice_index]
            clickables = cookie_notice.get('clickables', [])
            if len(clickables) > click.clickable_index:
                clickable = clickables[click.clickable_index]
                self.recordNewPagesForClick = True
                self._click_node(clickable.get('node_id'))
                self.tab.wait(1)

                # if the frame started loading a new page, we wait
                if self.waitForNavigatedEvent:
                    self._wait_for_load_event(30)


                is_page_modal = self.is_page_modal({
                        'x': cookie_notice.get('x'),
                        'y': cookie_notice.get('y'),
                        'width': cookie_notice.get('width'),
                        'height': cookie_notice.get('height'),
                    })
                self.click_result.set_is_page_modal(is_page_modal)

                # check whether cookie notice is still visible
                if self._does_node_exist(cookie_notice.get('node_id')):
                    is_cookie_notice_visible = self.is_node_visible(cookie_notice.get('node_id')).get('is_visible')
                    self.click_result.set_cookie_notice_visible_after_click(is_cookie_notice_visible)
                else:
                    self.click_result.set_cookie_notice_visible_after_click(False)

        # get cookies
        self.click_result.set_cookies('after_click', self._get_all_cookies())


    ############################################################################
    # COOKIE NOTICES
    ############################################################################

    def detect_cookie_notices(self, take_screenshots=True):
        # check whether the consent management platform is used
        # -> there should be a cookie notice
        is_cmp_defined = self.is_cmp_function_defined()
        self.result.set_cmp_defined(is_cmp_defined)

        # find cookie notice by using AdblockPlus rules
        cookie_notice_filters = {}
        for abp_filter_name, abp_filter in self.abp_filters.items():
            cookie_notice_rule_node_ids = set(self.find_cookie_notices_by_rules(abp_filter))
            cookie_notice_rule_node_ids = self._filter_visible_nodes(cookie_notice_rule_node_ids)
            self.result.add_cookie_notices(abp_filter_name, self.get_properties_of_cookie_notices(cookie_notice_rule_node_ids))
            cookie_notice_filters[abp_filter_name] = cookie_notice_rule_node_ids

        # find string `cookie` in nodes and store the closest parent block element
        cookie_node_ids = self.search_for_string('cookie')
        cookie_node_ids = set([self.find_parent_block_element(node_id) for node_id in cookie_node_ids])
        cookie_node_ids = [cookie_node_id for cookie_node_id in cookie_node_ids if cookie_node_id is not None]
        cookie_node_ids = self._filter_visible_nodes(cookie_node_ids)

        # find fixed parent nodes (i.e. having style `position: fixed`) with string `cookie`
        cookie_notice_fixed_node_ids = self.find_cookie_notices_by_fixed_parent(cookie_node_ids)
        cookie_notice_fixed_node_ids = self._filter_visible_nodes(cookie_notice_fixed_node_ids)
        self.result.add_cookie_notices('fixed_parent', self.get_properties_of_cookie_notices(cookie_notice_fixed_node_ids))

        # find full-width parent nodes with string `cookie`
        cookie_notice_full_width_node_ids = self.find_cookie_notices_by_full_width_parent(cookie_node_ids)
        cookie_notice_full_width_node_ids = self._filter_visible_nodes(cookie_notice_full_width_node_ids)
        self.result.add_cookie_notices('full_width_parent', self.get_properties_of_cookie_notices(cookie_notice_full_width_node_ids))

        if take_screenshots:
            #self.tab.Page.bringToFront()
            self.take_screenshot('original')
            for filter_name, cookie_notice_filter_node_ids in cookie_notice_filters.items():
                self.take_screenshots_of_visible_nodes(cookie_notice_filter_node_ids, f'filter-{filter_name}')
            self.take_screenshots_of_visible_nodes(cookie_notice_fixed_node_ids, 'fixed_parent')
            self.take_screenshots_of_visible_nodes(cookie_notice_full_width_node_ids, 'full_width_parent')

    def get_properties_of_cookie_notices(self, node_ids):
        return [self._get_properties_of_cookie_notice(node_id) for node_id in node_ids]

    def _get_properties_of_cookie_notice(self, node_id):
        js_function = """
            function getCookieNoticeProperties(elem) {
                if (!elem) elem = this;
                const style = getComputedStyle(elem);

                // Source: https://codereview.stackexchange.com/a/141854
                function powerset(l) {
                    return (function ps(list) {
                        if (list.length === 0) {
                            return [[]];
                        }
                        var head = list.pop();
                        var tailPS = ps(list);
                        return tailPS.concat(tailPS.map(function(e) { return [head].concat(e); }));
                    })(l.slice());
                }

                function getUniqueClassCombinations(elem) {
                    let result = [];
                    let classCombinations = powerset(Array.from(elem.classList));
                    for (var i = 0; i < classCombinations.length; i++) {
                        let classCombination = classCombinations[i];
                        if (classCombination.length == 0) {
                            continue;
                        }
                        if (document.getElementsByClassName(classCombination.join(' ')).length == 1) {
                            result.push(classCombination.join(' '));
                        }
                    }
                    return result;
                }

                function getUniqueAttributeCombinations(elem) {
                    function removeFromArray(array, item) {
                        const index = array.indexOf(item);
                        if (index > -1) {
                            array.splice(index, 1);
                        }
                    }

                    let attributes = Array.from(elem.attributes);
                    let attributeNames = [];
                    for (var i = 0; i < attributes.length; i++) {
                        let attributeName = attributes[i].localName;
                        if (attributeName == 'id' || attributeName == 'class' || attributeName == 'style') {
                            continue;
                        }
                        attributeNames.push(attributeName);
                    }

                    let result = [];
                    let attributeCombinations = powerset(attributeNames);
                    for (var i = 0; i < attributeCombinations.length; i++) {
                        let attributeCombination = attributeCombinations[i];
                        if (attributeCombination.length == 0) {
                            continue;
                        }

                        let selector = '';
                        for (var j = 0; j < attributeCombination.length; j++) {
                            let attributeName = attributeCombination[j];
                            let attributeValue = elem.getAttribute(attributeName);
                            selector += '[' + attributeName + '="' + attributeValue.replace(/"/g, '\\\\"') + '"]';
                        }
                        console.log(selector);
                        if (document.querySelectorAll(selector).length == 1) {
                            result.push(attributeCombination.join(' '));
                        }
                    }
                    return result;
                }

                let width = elem.offsetWidth;
                if (width >= document.documentElement.clientWidth) {
                    width = 'full';
                }
                let height = elem.offsetHeight;
                if (height >= document.documentElement.clientHeight) {
                    height = 'full';
                }

                return {
                    'html': elem.outerHTML,
                    'has_id': elem.hasAttribute('id'),
                    'has_class': elem.hasAttribute('class'),
                    'unique_class_combinations': getUniqueClassCombinations(elem),
                    'unique_attribute_combinations': getUniqueAttributeCombinations(elem),
                    'id': elem.getAttribute('id'),
                    'class': Array.from(elem.classList),
                    'text': elem.innerText,
                    'fontsize': style.fontSize,
                    'width': width,
                    'height': height,
                    'x': elem.getBoundingClientRect().left,
                    'y': elem.getBoundingClientRect().top,
                };
            }"""

        try:
            clickables = self.find_clickables_in_node(node_id)
            clickables_properties = self.get_properties_of_clickables(clickables)

            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            cookie_notice_properties = self._get_object_for_remote_object(result.get('objectId'))
            cookie_notice_properties['node_id'] = node_id
            cookie_notice_properties['clickables'] = clickables_properties
            cookie_notice_properties['is_page_modal'] = self.is_page_modal({
                    'x': cookie_notice_properties.get('x'),
                    'y': cookie_notice_properties.get('y'),
                    'width': cookie_notice_properties.get('width'),
                    'height': cookie_notice_properties.get('height'),
                })
            return cookie_notice_properties
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_get_cookie_notice_properties',
            })
            return dict.fromkeys([
                    'html', 'has_id', 'has_class', 'unique_class_combinations',
                    'unique_attribute_combinations', 'id', 'class', 'text',
                    'fontsize', 'width', 'height', 'x', 'y'])


    ############################################################################
    # GENERAL
    ############################################################################

    def detect_language(self):
        try:
            result = self.tab.Runtime.evaluate(expression='document.body.innerText').get('result')
            language = detect(result.get('value'))
            self.result.set_language(language)
        except Exception as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'detect_language',
            })

    def search_for_string(self, search_string):
        """Searches the DOM for the given string and returns all found nodes."""

        # stop execution of scripts to ensure that results do not change during search
        self.tab.Emulation.setScriptExecutionDisabled(value=True)

        # search for the string in a text node
        # take the parent of the text node (the element that contains the text)
        # this is necessary if an element contains more than one text node!
        # see for explanation:
        # - https://stackoverflow.com/a/2994336
        # - https://stackoverflow.com/a/11744783
        search_object = self.tab.DOM.performSearch(
                query="//body//*/text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + search_string + "')]/parent::*")

        node_ids = []
        if search_object.get('resultCount') != 0:
            search_results = self.tab.DOM.getSearchResults(
                    searchId=search_object.get('searchId'),
                    fromIndex=0,
                    toIndex=int(search_object.get('resultCount')))
            node_ids = search_results.get('nodeIds')

        # remove script and style nodes
        node_ids = [node_id for node_id in node_ids if not self._is_script_or_style_node(node_id)]

        # resume execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=False)

        # return nodes
        return node_ids

    def find_parent_block_element(self, node_id):
        """Returns the nearest parent block element or the element itself if it is a block element."""

        js_function = """
            function findClosestBlockElement(elem) {
                function isInlineElement(elem) {
                    const style = getComputedStyle(elem);
                    return style.display == 'inline';
                }

                if (!elem) elem = this;
                while(elem && elem !== document.body && isInlineElement(elem)) {
                    elem = elem.parentNode;
                }
                return elem;
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return self._get_node_id_for_remote_object(result.get('objectId'))
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'find_parent_block_element',
            })
            return None


    ############################################################################
    # COOKIE NOTICE DETECTION: FULL WIDTH PARENT
    ############################################################################

    def find_cookie_notices_by_full_width_parent(self, cookie_node_ids):
        cookie_notice_full_width_node_ids = set()
        for node_id in cookie_node_ids:
            fwp_result = self._find_full_width_parent(node_id)
            if fwp_result.get('parent_node_exists'):
                cookie_notice_full_width_node_ids.add(fwp_result.get('parent_node'))
        return cookie_notice_full_width_node_ids

    def _find_full_width_parent(self, node_id):
        js_function = """
            function findFullWidthParent(elem) {
                function parseValue(value) {
                    var parsedValue = parseInt(value);
                    if (isNaN(parsedValue)) {
                        return 0;
                    } else {
                        return parsedValue;
                    }
                }

                function getWidth(elem) {
                    const style = getComputedStyle(elem);
                    return elem.clientWidth +
                        parseValue(style.borderLeftWidth) + parseValue(style.borderRightWidth) +
                        parseValue(style.marginLeft) + parseValue(style.marginRight);
                }

                function getHeight(elem) {
                    const style = getComputedStyle(elem);
                    return elem.clientHeight +
                        parseValue(style.borderTopWidth) + parseValue(style.borderBottomWidth) +
                        parseValue(style.marginTop) + parseValue(style.marginBottom);
                }

                function getVerticalSpacing(elem) {
                    const style = getComputedStyle(elem);
                    return parseValue(style.paddingTop) + parseValue(style.paddingBottom) +
                        parseValue(style.borderTopWidth) + parseValue(style.borderBottomWidth) +
                        parseValue(style.marginTop) + parseValue(style.marginBottom);
                }

                function getHeightDiff(outerElem, innerElem) {
                    return getHeight(outerElem) - getHeight(innerElem);
                }

                function isParentHigherThanItsSpacing(outerElem, innerElem) {
                    let allowedIncrease = Math.max(0.25*getHeight(innerElem), 20);
                    return getHeightDiff(outerElem, innerElem) > (getVerticalSpacing(outerElem) + allowedIncrease);
                }

                function getPosition(elem) {
                    return elem.getBoundingClientRect().top;
                }

                function getPositionDiff(outerElem, innerElem) {
                    return Math.abs(getPosition(outerElem) - getPosition(innerElem));
                }

                function getPositionSpacing(outerElem, innerElem) {
                    const outerStyle = getComputedStyle(outerElem);
                    const innerStyle = getComputedStyle(innerElem);
                    return parseValue(innerStyle.marginTop) +
                        parseValue(outerStyle.paddingTop) + parseValue(outerStyle.borderTopWidth)
                }

                function isParentMovedMoreThanItsSpacing(outerElem, innerElem) {
                    let allowedIncrease = Math.max(0.25*getHeight(innerElem), 20);
                    return getPositionDiff(outerElem, innerElem) > (getPositionSpacing(outerElem, innerElem) + allowedIncrease);
                }

                if (!elem) elem = this;
                while(elem && elem !== document.body) {
                    parent = elem.parentNode;
                    if (isParentHigherThanItsSpacing(parent, elem) || isParentMovedMoreThanItsSpacing(parent, elem)) {
                        break;
                    }
                    elem = parent;
                }

                let allowedIncrease = 18; // for scrollbar issues
                if (document.documentElement.clientWidth <= (getWidth(elem) + allowedIncrease)) {
                    return elem;
                } else {
                    return false;
                }
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')

            # if a boolean is returned, we did not find a full-width small parent
            if result.get('type') == 'boolean':
                return {
                    'parent_node_exists': result.get('value'),
                    'parent_node': None,
                }
            # otherwise, we found one
            else:
                return {
                    'parent_node_exists': True,
                    'parent_node': self._get_node_id_for_remote_object(result.get('objectId')),
                }
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_find_full_width_parent',
            })
            return {
                'parent_node_exists': False,
                'parent_node': None,
            }


    ############################################################################
    # COOKIE NOTICE DETECTION: FIXED PARENT
    ############################################################################

    def find_cookie_notices_by_fixed_parent(self, cookie_node_ids):
        cookie_notice_fixed_node_ids = set()
        for node_id in cookie_node_ids:
            fp_result = self._find_fixed_parent(node_id)
            if fp_result.get('has_fixed_parent'):
                cookie_notice_fixed_node_ids.add(fp_result.get('fixed_parent'))
        return cookie_notice_fixed_node_ids

    def _find_fixed_parent(self, node_id):
        js_function = """
            function findFixedParent(elem) {
                if (!elem) elem = this;
                while(elem && elem.parentNode !== document) {
                    let style = getComputedStyle(elem);
                    if (style.position === 'fixed') {
                        return elem;
                    }
                    elem = elem.parentNode;
                }
                return elem; // html node
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            result_node_id = self._get_node_id_for_remote_object(result.get('objectId'))

            # if the returned parent element is an html element,
            # no fixed parent element was found
            if self._is_html_node(result_node_id):
                html_node_id = result_node_id
                html_node = self.tab.DOM.describeNode(nodeId=html_node_id).get('node')

                # if the html element is the root html element, we have not found
                # a fixed parent
                if self._get_root_frame_id() == html_node.get('frameId'):
                    return {
                        'has_fixed_parent': False,
                        'fixed_parent': None,
                    }
                # otherwise, the frame is considered to be the fixed parent
                else:
                    frame_node_id = self.tab.DOM.getFrameOwner(frameId=html_node.get('frameId')).get('nodeId')
                    return {
                        'has_fixed_parent': True,
                        'fixed_parent': frame_node_id,
                    }
            # otherwise, the returned parent element is a fixed element
            else:
                return {
                    'has_fixed_parent': True,
                    'fixed_parent': result_node_id,
                }
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_find_fixed_parent',
            })
            return {
                'has_fixed_parent': False,
                'fixed_parent': None,
            }


    ############################################################################
    # COOKIE NOTICE DETECTION: RULES
    ############################################################################

    def find_cookie_notices_by_rules(self, abp_filter):
        """Returns the node ids of the found cookie notices.

        The function uses the AdblockPlus ruleset of the browser plugin
        `I DON'T CARE ABOUT COOKIES`.
        See: https://www.i-dont-care-about-cookies.eu/
        """
        rules = [rule.selector.get('value') for rule in abp_filter.get_applicable_rules(self.webpage.domain)]
        rules_js = json.dumps(rules)

        js_function = """
            (function() {
                let rules = """ + rules_js + """;
                let cookie_notices = [];

                rules.forEach(function(rule) {
                    let elements = document.querySelectorAll(rule);
                    elements.forEach(function(element) {
                        cookie_notices.push(element);
                    });
                });

                return cookie_notices;
            })();"""

        query_result = self.tab.Runtime.evaluate(expression=js_function).get('result')
        return self._get_array_of_node_ids_for_remote_object(query_result.get('objectId'))


    ############################################################################
    # COOKIE NOTICE DETECTION: CMP
    ############################################################################

    def is_cmp_function_defined(self):
        """Checks whether the function `__cmp` is defined on the JavaScript `window` object."""
        result = self.tab.Runtime.evaluate(expression="typeof window.__cmp !== 'undefined'").get('result')
        return result.get('value')


    ############################################################################
    # CLICKABLES
    ############################################################################

    def find_clickables_in_node(self, node_id):
        # getEventListeners()
        # https://developers.google.com/web/tools/chrome-devtools/console/utilities?utm_campaign=2016q3&utm_medium=redirect&utm_source=dcc#geteventlistenersobject

        js_function = """
            function findClickablesInElement(elem) {
                function findCoveringNodes(nodes) {
                    let covering_nodes = Array.from(nodes);

                    for (var i = 0; i < nodes.length; i++) {
                        let node1 = nodes[i];
                        for (var j = 0; j < nodes.length; j++) {
                            let node2 = nodes[j];
                            // check whether node2 is contained in node1, if yes remove
                            if (node1 !== node2 && node1.contains(node2)) {
                                const index = covering_nodes.indexOf(node2);
                                covering_nodes.splice(index, 1);
                            }
                        }
                    }
                    return covering_nodes;
                }

                if (!elem) elem = this;
                let nodes = elem.querySelectorAll('a, button, input[type="button"], input[type="submit"], [role="button"], [role="link"]');
                return findCoveringNodes(nodes);
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return self._get_array_of_node_ids_for_remote_object(result.get('objectId'))
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'find_clickables_in_node',
            })
            return []

    def get_properties_of_clickables(self, node_ids):
        return [self._get_properties_of_clickable(node_id) for node_id in node_ids]

    def _get_properties_of_clickable(self, node_id):
        js_function = """
            function getPropertiesOfClickable(elem) {
                if (!elem) elem = this;

                const style = getComputedStyle(elem);

                let clickable_type;
                if (elem.localName == 'a' || elem.getAttribute('role') == 'link') {
                    clickable_type = 'link';
                } else {
                    clickable_type = 'button';
                }

                return {
                    'html': elem.outerHTML,
                    'node': elem.localName,
                    'type': clickable_type,
                    'text': elem.innerText,
                    'value': elem.getAttribute('value'),
                    'fontsize': style.fontSize,
                    'width': elem.offsetWidth,
                    'height': elem.offsetHeight,
                    'x': elem.getBoundingClientRect().left,
                    'y': elem.getBoundingClientRect().top,
                };
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            properties_of_clickable = self._get_object_for_remote_object(result.get('objectId'))
            properties_of_clickable['node_id'] = node_id
            properties_of_clickable['is_visible'] = self.is_node_visible(node_id).get('is_visible')
            return properties_of_clickable
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_get_cookie_notice_properties',
            })
            return dict.fromkeys(['html', 'node', 'type', 'text', 'value', 'fontsize', 'width', 'height', 'x', 'y', 'is_visible'])

    def _click_node(self, node_id):
        js_function = """
            function clickNode(elem) {
                if (!elem) elem = this;
                elem.click();
            }"""

        try:
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return True
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_click_node',
            })
            return False


    ############################################################################
    # NODE VISIBILITY
    ############################################################################

    def _filter_visible_nodes(self, node_ids):
        return [node_id for node_id in node_ids if self.is_node_visible(node_id).get('is_visible')]

    def is_node_visible(self, node_id):
        # Source: https://stackoverflow.com/a/41698614
        # adapted to also look at child nodes (especially important for fixed 
        # elements as they might not be "visible" themselves when they have no 
        # width or height)
        js_function = """
            function isVisible(elem) {
                if (!elem) elem = this;
                if (!(elem instanceof Element)) return false;
                let visible = true;
                const style = getComputedStyle(elem);

                // for these rules the childs cannot be visible, directly return
                if (style.display === 'none') return false;
                if (style.opacity < 0.1) return false;
                if (style.visibility !== 'visible') return false;

                // for these rules a child element might still be visible,
                // we need to also look at the childs, no direct return
                if (elem.offsetWidth + elem.offsetHeight + elem.getBoundingClientRect().height +
                    elem.getBoundingClientRect().width === 0) {
                    visible = false;
                }
                if (elem.offsetWidth < 10 || elem.offsetHeight < 10) {
                    visible = false;
                }
                const elemCenter = {
                    x: elem.getBoundingClientRect().left + elem.offsetWidth / 2,
                    y: elem.getBoundingClientRect().top + elem.offsetHeight / 2
                };
                if (elemCenter.x < 0) visible = false;
                if (elemCenter.x > (document.documentElement.clientWidth || window.innerWidth)) visible = false;
                if (elemCenter.y < 0) visible = false;
                if (elemCenter.y > (document.documentElement.clientHeight || window.innerHeight)) visible = false;

                if (visible) {
                    let pointContainer = document.elementFromPoint(elemCenter.x, elemCenter.y);
                    do {
                        if (pointContainer === elem) return elem;
                        if (!pointContainer) break;
                    } while (pointContainer = pointContainer.parentNode);
                }

                // check the child nodes
                if (!visible) {
                    let childrenCount = elem.childNodes.length;
                    for (var i = 0; i < childrenCount; i++) {
                        let isChildVisible = isVisible(elem.childNodes[i]);
                        if (isChildVisible) {
                            return isChildVisible;
                        }
                    }
                }

                return false;
            }"""

        # the function `isVisible` is calling itself recursively, 
        # therefore it needs to be defined beforehand
        self.tab.Runtime.evaluate(expression=js_function)

        try:
            # call the function `isVisible` on the node
            remote_object_id = self._get_remote_object_id_by_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')

            # if a boolean is returned, the object is not visible
            if result.get('type') == 'boolean':
                return {
                    'is_visible': result.get('value'),
                    'visible_node': None,
                }
            # otherwise, the object or one of its children is visible
            else:
                return {
                    'is_visible': True,
                    'visible_node': self._get_node_id_for_remote_object(result.get('objectId')),
                }
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'is_node_visible',
            })
            return {
                'is_visible': False,
                'visible_node': None,
            }


    ############################################################################
    # MODALITY CHECK
    ############################################################################

    def is_page_modal(self, cookie_notice=None):
        cookie_notice_js = json.dumps(cookie_notice)

        js_function = """
            (function modal() {
                let margin = 5;
                let cookieNotice = """ + cookie_notice_js + """;

                let viewportWidth = document.documentElement.clientWidth;
                let viewportHeight = document.documentElement.clientHeight;
                let viewportHorizontalCenter = viewportWidth / 2;
                let viewportVerticalCenter = viewportHeight / 2;

                let testPositions = [
                    {'x': margin, 'y': margin},
                    {'x': margin, 'y': viewportVerticalCenter},
                    {'x': margin, 'y': viewportHeight - margin},
                    {'x': viewportVerticalCenter, 'y': margin},
                    {'x': viewportVerticalCenter, 'y': viewportHeight - margin},
                    {'x': viewportWidth - margin, 'y': margin},
                    {'x': viewportWidth - margin, 'y': viewportVerticalCenter},
                    {'x': viewportWidth - margin, 'y': viewportHeight - margin},
                ];

                if (cookieNotice) {
                    if (cookieNotice.width == 'full') {
                        cookieNotice.width = viewportWidth;
                    }
                    if (cookieNotice.height == 'full') {
                        cookieNotice.height = viewportHeight;
                    }
                    for (var i = 0; i < testPositions.length; i++) {
                        let testPosition = testPositions[i];
                        if ((testPosition.x >= cookieNotice.x && testPosition.x <= (cookieNotice.x + cookieNotice.width)) &&
                                (testPosition.y >= cookieNotice.y && testPosition.y <= (cookieNotice.y + cookieNotice.height))) {
                            let index = testPositions.indexOf(testPosition);
                            testPositions.splice(index, 1);
                        }
                    }
                }

                let previousContainer = document.elementFromPoint(testPositions[0].x, testPositions[0].y);
                for (var i = 1; i < testPositions.length; i++) {
                    let testPosition = testPositions[i];
                    let testContainer = document.elementFromPoint(testPosition.x, testPosition.y);
                    if (previousContainer !== testContainer) {
                        return false;
                    }
                    previousContainer = testContainer;
                }
                return true;
            })();"""

        result = self.tab.Runtime.evaluate(expression=js_function).get('result')
        return result.get('value')


    ############################################################################
    # SCREENSHOTS
    ############################################################################

    def take_screenshots_of_visible_nodes(self, node_ids, name):
        # filter only visible nodes
        # and replace the original node_id with their visible children if the node itself is not visible
        node_ids = [visibility.get('visible_node') for visibility in (self.is_node_visible(node_id) for node_id in node_ids) 
                    if visibility and visibility.get('is_visible')]
        self.take_screenshots_of_nodes(node_ids, name)

    def take_screenshots_of_nodes(self, node_ids, name):
        # take a screenshot of the page with every node highlighted
        for index, node_id in enumerate(node_ids):
            self._highlight_node(node_id)
            self.take_screenshot(name + '-' + str(index))
            self._hide_highlight()

    def take_screenshot(self, name):
        # get the width and height of the viewport
        layout_metrics = self.tab.Page.getLayoutMetrics()
        viewport = layout_metrics.get('layoutViewport')
        width = viewport.get('clientWidth')
        height = viewport.get('clientHeight')
        x = viewport.get('pageX')
        y = viewport.get('pageY')
        screenshot_viewport = {'x': x, 'y': y, 'width': width, 'height': height, 'scale': 1}

        # take screenshot and store it
        self.result.add_screenshot(name, self.tab.Page.captureScreenshot(clip=screenshot_viewport)['data'])

    def _highlight_node(self, node_id):
        """Highlight the given node with an overlay."""
        color_content = {'r': 152, 'g': 196, 'b': 234, 'a': 0.5}
        color_padding = {'r': 184, 'g': 226, 'b': 183, 'a': 0.5}
        color_margin = {'r': 253, 'g': 201, 'b': 148, 'a': 0.5}
        highlightConfig = {'contentColor': color_content, 'paddingColor': color_padding, 'marginColor': color_margin}
        self.tab.Overlay.highlightNode(highlightConfig=highlightConfig, nodeId=node_id)

    def _hide_highlight(self):
        self.tab.Overlay.hideHighlight()


    ############################################################################
    # REMOTE OBJECTS
    ############################################################################

    def _get_node_id_for_remote_object(self, remote_object_id):
        return self.tab.DOM.requestNode(objectId=remote_object_id).get('nodeId')

    def _get_array_of_node_ids_for_remote_object(self, remote_object_id):
        array_attributes = self._get_properties_of_remote_object(remote_object_id)
        remote_object_ids = [array_element.get('value').get('objectId') for array_element in array_attributes if array_element.get('enumerable')]
        node_ids = []
        for remote_object_id in remote_object_ids:
            try:
                node_ids.append(self._get_node_id_for_remote_object(remote_object_id))
            except pychrome.exceptions.CallMethodException as e:
                self.result.add_warning({
                    'message': str(e),
                    'exception': type(e).__name__,
                    'traceback': traceback.format_exc().splitlines(),
                    'method': '_get_array_of_node_ids_for_remote_object',
                })
        return node_ids

    def _get_object_for_remote_object(self, remote_object_id):
        object_attributes = self._get_properties_of_remote_object(remote_object_id)
        result = {
                attribute.get('name'): attribute.get('value').get('value')
                for attribute in object_attributes
                if self._is_remote_attribute_a_primitive(attribute)
            }

        # search for nested objects
        result.update({
                attribute.get('name'): self._get_object_for_remote_object(attribute.get('value').get('objectId'))
                for attribute in object_attributes
                if self._is_remote_attribute_an_object(attribute)
            })

        # search for nested arrays
        result.update({
                attribute.get('name'): self._get_array_for_remote_object(attribute.get('value').get('objectId'))
                for attribute in object_attributes
                if self._is_remote_attribute_an_array(attribute)
            })

        return result

    def _get_array_for_remote_object(self, remote_object_id):
        array_attributes = self._get_properties_of_remote_object(remote_object_id)
        return [
                array_element.get('value').get('value')
                for array_element in array_attributes
                if array_element.get('enumerable')
            ]

    def _is_remote_attribute_a_primitive(self, attribute):
        return attribute.get('enumerable') \
               and attribute.get('value').get('type') != 'object' \
               or attribute.get('value').get('subtype', '') == 'null'

    def _is_remote_attribute_an_object(self, attribute):
        return attribute.get('enumerable') \
               and attribute.get('value').get('type') == 'object' \
               and attribute.get('value').get('subtype', '') != 'array' \
               and attribute.get('value').get('subtype', '') != 'null'

    def _is_remote_attribute_an_array(self, attribute):
        return attribute.get('enumerable') \
               and attribute.get('value').get('type') == 'object' \
               and attribute.get('value').get('subtype', '') == 'array'

    def _get_properties_of_remote_object(self, remote_object_id):
        return self.tab.Runtime.getProperties(objectId=remote_object_id, ownProperties=True).get('result')

    def _get_remote_object_id_by_node_id(self, node_id):
        try:
            return self.tab.DOM.resolveNode(nodeId=node_id).get('object').get('objectId')
        except Exception:
            return None


    ############################################################################
    # NODE DATA
    ############################################################################

    def _does_node_exist(self, node_id):
        try:
            self.tab.DOM.describeNode(nodeId=node_id)
            return True
        except Exception:
            return False

    def _get_root_frame_id(self):
        return self.tab.Page.getFrameTree().get('frameTree').get('frame').get('id')

    def _get_html_of_node(self, node_id):
        return self.tab.DOM.getOuterHTML(nodeId=node_id).get('outerHTML')

    def _get_node_name(self, node_id):
        try:
            return self.tab.DOM.describeNode(nodeId=node_id).get('node').get('nodeName').lower()
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': '_get_node_name',
            })
            return None

    def _is_script_or_style_node(self, node_id):
        node_name = self._get_node_name(node_id)
        return node_name == 'script' or node_name == 'style'

    def _is_html_node(self, node_id):
        return self._get_node_name(node_id) == 'html'

    def _is_inline_element(self, node_id):
        inline_elements = [
            'a', 'abbr', 'acronym', 'b', 'bdo', 'big', 'br', 'button', 'cite',
            'code', 'dfn', 'em', 'i', 'img', 'input', 'kbd', 'label', 'map',
            'object', 'output', 'q', 'samp', 'script', 'select', 'small',
            'span', 'strong', 'sub', 'sup', 'textarea', 'time', 'tt', 'var'
        ]
        return self._get_node_name(node_id) in inline_elements


    ############################################################################
    # MISC
    ############################################################################

    def _scroll_down(self, delta_y):
        self.tab.Input.emulateTouchFromMouseEvent(type="mouseWheel", x=1, y=1, button="none", deltaX=0, deltaY=-1*delta_y)
        self.tab.wait(0.1)

    def _get_all_cookies(self):
        return self.tab.Network.getAllCookies().get('cookies')


if __name__ == '__main__':
    ARG_TOP_2000 = '1'
    ARG_RANDOM = '2'

    # the dataset is passed to the script as argument
    # (top 2000 domains or random sampled domains)
    # default is top 2000 domains
    parser = argparse.ArgumentParser(description='Scans a list of domains, identifies cookie notices and evaluates them.')
    parser.add_argument('--dataset', dest='dataset', nargs='?', default='1',
                        help=f'the set of domains to scan: ' +
                             f'`{ARG_TOP_2000}` for the top 2000 domains, ' +
                             f'`{ARG_RANDOM}` for domains in file `resources/sampled-domains.txt`')
    parser.add_argument('--results', dest='results_directory', nargs='?', default='results',
                        help='the directory to store the the results in ' +
                             '(default: `results`)')
    parser.add_argument('--click', dest='do_click', action="store_true",
                        help='whether all links and buttons in the detected cookie notices should be clicked or not ' +
                             '(default: false)')

    # load the correct dataset
    args = parser.parse_args()
    if args.dataset == ARG_TOP_2000:
        tranco = Tranco(cache=True, cache_dir='tranco')
        tranco_list = tranco.list(date='2020-03-01')
        domains = tranco_list.top(2000)
    else:
        domains = []
        with open('resources/sampled-domains.txt') as f:
            domains = [line.strip() for line in f]

    # create multiprocessor pool:
    # currently only one tab is processed at a time -> not parallel
    pool = mp.Pool(1)

    # create the browser and a helper function to scan pages
    browser = Browser(abp_filter_filenames=['resources/easylist-cookie.txt', 'resources/i-dont-care-about-cookies.txt'])
    f_scan_page = partial(Browser.scan_page, browser)

    # create results directory if necessary
    os.makedirs(args.results_directory, exist_ok=True)

    # this is a callback function that is called when scanning a page finished
    def f_page_scanned(result):
        # cookies are not correct if pages are scanned in parallel
        #result.exclude_field_from_json('cookies')

        # save results and screenshots
        result.save_data(args.results_directory)
        result.save_screenshots(args.results_directory)

        # ocr with tesseract
        #subprocess.call(["tesseract", result.screenshot_filename, result.ocr_filename, "--oem", "1", "-l", "eng+deu"])

        print(f'#{str(result.rank)}: {result.url}')
        if result.stopped_waiting:
            print(f'-> stopped waiting for {result.stopped_waiting_reason}')
        if result.failed:
            print(f'-> failed: {result.failed_reason}' + (f' ({result.failed_exception})' if result.failed_exception is not None else ''))
            if result.failed_traceback is not None:
                print(result.failed_traceback)

    # scan the pages in parallel
    for rank, domain in enumerate(domains, start=1):
        webpage = Webpage(rank=rank, domain=domain)
        pool.apply_async(f_scan_page, args=(webpage, args.do_click), callback=f_page_scanned)

    # close pool
    pool.close()
    pool.join()
