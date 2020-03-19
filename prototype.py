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
from tld import get_fld
from tranco import Tranco


# Possible improvements:
# - if cookie notice is displayed in iframe (e.g. forbes.com), currently the 
# iframe is assumed to be the cookie notice; one might even walk up the tree 
# even further and find a fixed or full-width parent there


class Webpage:
    def __init__(self, rank=None, hostname='', protocol='https'):
        self.rank = rank
        self.hostname = hostname
        self.protocol = protocol
        self.url = f'{self.protocol}://{self.hostname}'

    def set_protocol(self, protocol):
        self.protocol = protocol
        self.url = f'{self.protocol}://{self.hostname}'

    def set_subdomain(self, subdomain):
        self.url = f'{self.protocol}://{subdomain}.{self.hostname}'

    def remove_subdomain(self):
        self.url = f'{self.protocol}://{self.hostname}'


FAILED_REASON_TIMEOUT = 'Page.navigate timeout'
FAILED_REASON_STATUS_CODE = 'status code'
FAILED_REASON_LOADING = 'loading failed'


class WebpageResult:
    def __init__(self, webpage):
        self.rank = webpage.rank
        self.hostname = webpage.hostname
        self.protocol = webpage.protocol
        self.url = webpage.url

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
        return f'{self.rank}-{self.hostname}-{name}.png'

    def save_data(self, directory):
        with open(f'{directory}/{self._get_filename_for_data()}', 'w') as file:
            file.write(self._to_json())

    def _get_filename_for_data(self):
        return f'{self.rank}-{self.hostname}.json'

    def _to_json(self):
        results = {k: v for k, v in self.__dict__.items() if k not in self._json_excluded_fields}
        return json.dumps(results, indent=4, default=lambda o: o.__dict__)

    def exclude_field_from_json(self, excluded_field):
        self._json_excluded_fields.append(excluded_field)


class WebpageCrawler:
    def __init__(self, tab, abp_filter, webpage):
        self.tab = tab
        self.abp_filter = abp_filter
        self.webpage = webpage
        self.result = WebpageResult(webpage)
        self.loaded_urls = []

    def get_result(self):
        return self.result

    def crawl(self):
        global lock_m, lock_n, lock_l

        # initialize `_is_loaded` variable to `False`
        # it will be set to `True` when the `loadEventFired` event occurs
        self._is_loaded = False

        # setup the tab
        self._setup_tab()
        self.tab.wait(0.1)
        self.requestId = None

        # deny permissions because they might pop-up and block detection
        self._deny_permissions()
        
        try:
            # open url: triple mutex
            #lock_n.acquire()
            #with lock_m:
            #    lock_n.release()
            self._clear_browser()
            self.tab.Page.bringToFront()
            self.tab.Page.navigate(url=self.webpage.url, _timeout=15)
            #self.tab.wait(1)

            # we wait for our load event to be fired (see `_event_load_event_fired`)
            waited = 0
            while not self._is_loaded and waited < 30:
                self.tab.wait(0.1)
                waited += 0.1

            if waited >= 30:
                self.result.set_stopped_waiting('load event')
                self.tab.Page.stopLoading()

            # return if failed to load page
            if self.result.failed:
                return self.result

            # wait for JavaScript code to be run, after the page has been loaded
            self.tab.wait(5)

            # bring to front: triple mutex
            #lock_n.acquire()
            #with lock_m:
            #    lock_n.release()
            #    self.tab.Page.bringToFront()
            #    self.tab.wait(3)

            # get root node of document, is needed to be sure that the DOM is loaded
            self.root_node = self.tab.DOM.getDocument().get('root')

            # detect cookie notices
            self.detect_cookie_notices()

            # get cookies
            self.result.set_cookies('all', self._get_all_cookies())
        except pychrome.exceptions.TimeoutException as e:
            self.result.set_failed(FAILED_REASON_TIMEOUT, type(e).__name__)
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

        return self.result

    def _setup_tab(self):
        # set callbacks for request and response logging
        self.tab.Network.requestWillBeSent = self._event_request_will_be_sent
        self.tab.Network.responseReceived = self._event_response_received
        self.tab.Network.loadingFailed = self._event_loading_failed
        self.tab.Page.loadEventFired = self._event_load_event_fired
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

    def _event_load_event_fired(self, timestamp, **kwargs):
        """Will be called when the page sends an load event.

        Note that this only means that all resources are loaded, the
        page may still process some JavaScript.
        """
        self._is_loaded = True

    def _event_javascript_dialog_opening(self, message, type, **kwargs):
        if type == 'alert':
            self.tab.Page.handleJavaScriptDialog(accept=True)
        else:
            self.tab.Page.handleJavaScriptDialog(accept=False)

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

    def detect_cookie_notices(self):
        global lock_m, lock_n, lock_l

        # store html of page
        self.result.set_html(self.get_html(self.root_node.get('nodeId')))

        # check whether language is english or german
        lang = self.detect_language()
        self.result.set_language(lang)

        # check whether the consent management platform is used
        # -> there should be a cookie notice
        is_cmp_defined = self.is_cmp_function_defined()
        self.result.set_cmp_defined(is_cmp_defined)

        # find cookie notice by using AdblockPlus rules
        cookie_notice_rule_node_ids = set(self.find_cookie_notices_by_rules())
        cookie_notice_rule_node_ids = self._filter_visible_nodes(cookie_notice_rule_node_ids)
        self.result.add_cookie_notices('rules', self.get_cookie_notice_data_of_nodes(cookie_notice_rule_node_ids))

        # find string `cookie` in nodes and store the closest parent block element
        cookie_node_ids = self.search_for_string('cookie')
        cookie_node_ids = set([self.find_parent_block_element(node_id) for node_id in cookie_node_ids])
        cookie_node_ids = [cookie_node_id for cookie_node_id in cookie_node_ids if cookie_node_id is not None]
        cookie_node_ids = self._filter_visible_nodes(cookie_node_ids)

        # find fixed parent nodes (i.e. having style `position: fixed`) with string `cookie`
        cookie_notice_fixed_node_ids = self.find_cookie_notices_by_fixed_parent(cookie_node_ids)
        cookie_notice_fixed_node_ids = self._filter_visible_nodes(cookie_notice_fixed_node_ids)
        self.result.add_cookie_notices('fixed-parent', self.get_cookie_notice_data_of_nodes(cookie_notice_fixed_node_ids))

        # find full-width parent nodes with string `cookie`
        cookie_notice_full_width_node_ids = self.find_cookie_notices_by_full_width_parent(cookie_node_ids)
        cookie_notice_full_width_node_ids = self._filter_visible_nodes(cookie_notice_full_width_node_ids)
        self.result.add_cookie_notices('full-width-parent', self.get_cookie_notice_data_of_nodes(cookie_notice_full_width_node_ids))

        # triple mutex
        #with lock_l:
        #    lock_n.acquire()
        #    with lock_m:
        #        lock_n.release()
        self.tab.Page.bringToFront()
        #self.tab.wait(1)
        self.take_screenshot('original')
        self.take_screenshots_of_visible_nodes(cookie_notice_rule_node_ids, 'rules')
        self.take_screenshots_of_visible_nodes(cookie_notice_fixed_node_ids, 'fixed-parent')
        self.take_screenshots_of_visible_nodes(cookie_notice_full_width_node_ids, 'full-width-parent')

    def get_cookie_notice_data_of_nodes(self, node_ids):
        return [{
                'html': self.get_html(node_id),
                'width': self.get_width(node_id),
                'height': self.get_height(node_id),
                'x': self.get_x(node_id),
                'y': self.get_y(node_id),
            } for node_id in node_ids]

    def get_html(self, node_id):
        return self.tab.DOM.getOuterHTML(nodeId=node_id).get('outerHTML')

    def get_width(self, node_id):
        """Returns the width of the visible (child) node
        or `full` if it takes the full width of the window.
        """

        js_function = """
            function getWidth(elem) {
                function parseValue(value) {
                    parsedValue = parseInt(value);
                    if (isNaN(parseValue)) {
                        return 0;
                    }
                    else {
                        return parseValue;
                    }
                }

                if (!elem) elem = this;
                const style = getComputedStyle(elem);
                width = elem.clientWidth + parseValue(style.borderLeftWidth) + parseValue(style.borderRightWidth);

                if (width === document.documentElement.clientWidth) {
                    return 'full';
                }
                else {
                    return width;
                }
            }"""

        try:
            visible_node_id = self.is_node_visible(node_id).get('visible_node')
            remote_object_id = self._get_remote_object_id_for_node_id(visible_node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return result.get('value')
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'get_width',
            })
            return None

    def get_height(self, node_id):
        """Returns the height of the visible (child) node
        or `full` if it takes the full height of the window.
        """

        js_function = """
            function getHeight(elem) {
                function parseValue(value) {
                    parsedValue = parseInt(value);
                    if (isNaN(parseValue)) {
                        return 0;
                    }
                    else {
                        return parseValue;
                    }
                }

                if (!elem) elem = this;
                const style = getComputedStyle(elem);
                height = elem.clientHeight + parseValue(style.borderTopWidth) + parseValue(style.borderBottomWidth);

                if (height === document.documentElement.clientHeight) {
                    return 'full';
                }
                else {
                    return height;
                }
            }"""

        try:
            visible_node_id = self.is_node_visible(node_id).get('visible_node')
            remote_object_id = self._get_remote_object_id_for_node_id(visible_node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return result.get('value')
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'get_height',
            })
            return None

    def get_x(self, node_id):
        js_function = """
            function getX(elem) {
                if (!elem) elem = this;
                return elem.getBoundingClientRect().top;
            }"""

        try:
            visible_node_id = self.is_node_visible(node_id).get('visible_node')
            remote_object_id = self._get_remote_object_id_for_node_id(visible_node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return result.get('value')
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'get_x',
            })
            return None

    def get_y(self, node_id):
        js_function = """
            function getY(elem) {
                if (!elem) elem = this;
                return elem.getBoundingClientRect().left;
            }"""

        try:
            visible_node_id = self.is_node_visible(node_id).get('visible_node')
            remote_object_id = self._get_remote_object_id_for_node_id(visible_node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return result.get('value')
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'get_y',
            })
            return None

    def detect_language(self):
        try:
            result = self.tab.Runtime.evaluate(expression='document.body.innerText').get('result')
            return detect(result.get('value'))
        except Exception as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'detect_language',
            })
            return None

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
        """Returns the nearest parent block element or the element itself if it
        is a block element."""

        # if the node is a block element, just return it again
        if not self._is_inline_element(node_id):
            return node_id

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
            # call the function `findClosestBlockElement` on the node
            remote_object_id = self._get_remote_object_id_for_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            return self._get_node_id_for_remote_object_id(result.get('objectId'))
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'find_parent_block_element',
            })
            return None

    def find_cookie_notices_by_full_width_parent(self, cookie_node_ids):
        cookie_notice_full_width_node_ids = set()
        for node_id in cookie_node_ids:
            fwp_result = self.find_full_width_parent(node_id)
            if fwp_result.get('parent_node_exists'):
                cookie_notice_full_width_node_ids.add(fwp_result.get('parent_node'))
        return cookie_notice_full_width_node_ids

    def find_full_width_parent(self, node_id):
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
            remote_object_id = self._get_remote_object_id_for_node_id(node_id)
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
                    'parent_node': self._get_node_id_for_remote_object_id(result.get('objectId')),
                }
        except pychrome.exceptions.CallMethodException as e:
            self.result.add_warning({
                'message': str(e),
                'exception': type(e).__name__,
                'traceback': traceback.format_exc().splitlines(),
                'method': 'find_full_width_parent',
            })
            return {
                'parent_node_exists': False,
                'parent_node': None,
            }

    def find_cookie_notices_by_fixed_parent(self, cookie_node_ids):
        cookie_notice_fixed_node_ids = set()
        for node_id in cookie_node_ids:
            fp_result = self.find_fixed_parent(node_id)
            if fp_result.get('has_fixed_parent'):
                cookie_notice_fixed_node_ids.add(fp_result.get('fixed_parent'))
        return cookie_notice_fixed_node_ids

    def find_fixed_parent(self, node_id):
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
            remote_object_id = self._get_remote_object_id_for_node_id(node_id)
            result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
            result_node_id = self._get_node_id_for_remote_object_id(result.get('objectId'))

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
                'method': 'find_fixed_parent',
            })
            return {
                'has_fixed_parent': False,
                'fixed_parent': None,
            }

    def find_cookie_notices_by_rules(self):
        """Returns the node ids of the found cookie notices.

        The function uses the AdblockPlus ruleset of the browser plugin
        `I DON'T CARE ABOUT COOKIES`.
        See: https://www.i-dont-care-about-cookies.eu/
        """

        rules = [rule.selector.get('value') for rule in self.abp_filter.get_applicable_rules(self.webpage.hostname)]
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
        array_result = self.tab.Runtime.getProperties(objectId=query_result.get('objectId'), ownProperties=True).get('result')
        remote_object_ids = [array_element.get('value').get('objectId') for array_element in array_result if array_element.get('enumerable')]

        cookie_notices = []
        for remote_object_id in remote_object_ids:
            try:
                cookie_notices.append(self._get_node_id_for_remote_object_id(remote_object_id))
            except pychrome.exceptions.CallMethodException as e:
                self.result.add_warning({
                    'message': str(e),
                    'exception': type(e).__name__,
                    'traceback': traceback.format_exc().splitlines(),
                    'method': 'find_cookie_notices_by_rules',
                })
        return cookie_notices

    def is_cmp_function_defined(self):
        """Checks whether the function `__cmp` is defined on the JavaScript
        `window` object."""

        result = self.tab.Runtime.evaluate(expression="typeof window.__cmp !== 'undefined'").get('result')
        return result.get('value')

    def find_clickables_in_node(self, node):
        pass
        #getEventListeners()
        # https://developers.google.com/web/tools/chrome-devtools/console/utilities?utm_campaign=2016q3&utm_medium=redirect&utm_source=dcc#geteventlistenersobject

    def is_node_visible(self, node_id):
        # Source: https://stackoverflow.com/a/41698614
        # adapted to also look at child nodes (especially important for fixed 
        # elements as they might not be "visible" themselves when they have no 
        # width or height)
        js_function = """
            function isVisible(elem) {
                if (!elem) elem = this;
                let visible = true;
                if (!(elem instanceof Element)) return false;
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
                if (elem.offsetWidth === 0 || elem.offsetHeight === 0) {
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

                if (visible === true) {
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
            remote_object_id = self._get_remote_object_id_for_node_id(node_id)
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
                    'visible_node': self._get_node_id_for_remote_object_id(result.get('objectId')),
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

    def take_screenshots_of_visible_nodes(self, node_ids, name):
        # filter only visible nodes
        # and replace the original node_id with their visible children if the node itself is not visible
        node_ids = [visibility.get('visible_node') for visibility in (self.is_node_visible(node_id) for node_id in node_ids) if visibility and visibility.get('is_visible')]
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

    def _scroll_down(self, delta_y):
        self.tab.Input.emulateTouchFromMouseEvent(type="mouseWheel", x=1, y=1, button="none", deltaX=0, deltaY=-1*delta_y)
        self.tab.wait(0.1)

    def _get_all_cookies(self):
        return self.tab.Network.getAllCookies().get('cookies')

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

    def _get_root_frame_id(self):
        return self.tab.Page.getFrameTree().get('frameTree').get('frame').get('id')

    def _get_node_id_for_remote_object_id(self, remote_object_id):
        return self.tab.DOM.requestNode(objectId=remote_object_id).get('nodeId')

    def _get_remote_object_id_for_node_id(self, node_id):
        try:
            remote_object_id = self.tab.DOM.resolveNode(nodeId=node_id).get('object').get('objectId')
        except Exception:
            remote_object_id = None
        return remote_object_id

    def _filter_visible_nodes(self, node_ids):
        return [node_id for node_id in node_ids if self.is_node_visible(node_id).get('is_visible')]

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


class Browser:
    def __init__(self, abp_filter_filename, debugger_url='http://127.0.0.1:9222'):
        # create a browser instance which controls chromium
        self.browser = pychrome.Browser(url=debugger_url)

        # create helpers
        self.abp_filter = AdblockPlusFilter(abp_filter_filename)

    def crawl_page(self, webpage):
        result = self._crawl_page(webpage)

        # try https with subdomain www
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.set_subdomain('www')
            result = self._crawl_page(webpage)
        # try http without subdomain www
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.remove_subdomain()
            webpage.set_protocol('http')
            result = self._crawl_page(webpage)
        # try http with www subdomain
        if result.failed and (result.failed_reason == FAILED_REASON_LOADING or result.failed_reason == FAILED_REASON_TIMEOUT):
            webpage.set_subdomain('www')
            result = self._crawl_page(webpage)

        return result

    def _crawl_page(self, webpage):
        global lock_m, lock_n, lock_l

        # triple mutex
        #lock_n.acquire()
        #with lock_m:
        #    lock_n.release()
        tab = self.browser.new_tab()

        page_crawler = WebpageCrawler(tab=tab, abp_filter=self.abp_filter, webpage=webpage)
        page_crawler.crawl()

        self.browser.close_tab(tab)
        return page_crawler.get_result()


class AdblockPlusFilter:
    def __init__(self, rules_filename):
        with open(rules_filename) as filterlist:
            # we only need filters with type css
            # other instances are Header, Metadata, etc.
            # other type is url-pattern which is used to block script files
            self._rules = [rule for rule in parse_filterlist(filterlist) if isinstance(rule, Filter) and rule.selector.get('type') == 'css']

    def get_applicable_rules(self, hostname):
        return [rule for rule in self._rules if self._is_rule_applicable(rule, hostname)]

    def _is_rule_applicable(self, rule, hostname):
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
            if opt_domain in hostname:
                return True
        return False


if __name__ == '__main__':
    ARG_TOP_2000 = '1'
    ARG_RANDOM = '2'

    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('--dataset', dest='dataset', nargs='?', default='1',
                        help=f'the dataset to scan (`{ARG_TOP_2000}` for top 2000 domains, `{ARG_RANDOM}` for random sample from `sampled-domains.txt`)')

    args = parser.parse_args()
    if args.dataset == ARG_TOP_2000:
        tranco = Tranco(cache=True, cache_dir='tranco')
        tranco_list = tranco.list(date='2020-03-01')
        domains = tranco_list.top(2000)
    else:
        domains = []
        with open('resources/sampled-domains.txt') as f:
            domains = [line.strip() for line in f]

    # triple mutex:
    # https://stackoverflow.com/a/11673600
    # https://stackoverflow.com/a/28721419
    lock_m = Lock()
    lock_n = Lock()
    lock_l = Lock()

    # create multiprocessor pool: twelve tabs are processed in parallel at most
    pool = mp.Pool(1)

    # create the browser and a helper function to crawl pages
    browser = Browser(abp_filter_filename='resources/cookie-notice-css-rules.txt')
    f_crawl_page = partial(Browser.crawl_page, browser)

    # create results directory if necessary
    RESULTS_DIRECTORY = 'results'
    os.makedirs(RESULTS_DIRECTORY, exist_ok=True)

    # this is a callback function that is called when crawling a page finished
    def f_page_crawled(result):
        # cookies are not correct if pages are crawled in parallel
        #result.exclude_field_from_json('cookies')

        # save results and screenshots
        result.save_data(RESULTS_DIRECTORY)
        result.save_screenshots(RESULTS_DIRECTORY)

        # ocr with tesseract
        #subprocess.call(["tesseract", result.screenshot_filename, result.ocr_filename, "--oem", "1", "-l", "eng+deu"])

        print(f'#{str(result.rank)}: {result.url}')
        if result.stopped_waiting:
            print(f'-> stopped waiting for {result.stopped_waiting_reason}')
        if result.failed:
            print(f'-> failed: {result.failed_reason}' + (f' ({result.failed_exception})' if result.failed_exception is not None else ''))
            if result.failed_traceback is not None:
                print(result.failed_traceback)

    # crawl the pages in parallel
    for rank, domain in enumerate(domains, start=1):
        webpage = Webpage(rank=rank, hostname=domain)
        pool.apply_async(f_crawl_page, args=(webpage,), callback=f_page_crawled)

    # close pool
    pool.close()
    pool.join()
