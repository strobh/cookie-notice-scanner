#!/usr/bin/env python3

import time
import pychrome
import base64
import subprocess

from pprint import pprint
from urllib.parse import urlparse

import abp.filters.parser
from abp.filters import parse_filterlist


class WebpageResult:
    def __init__(self, url=''):
        self.url = url
        
        parsed_url = urlparse(self.url)
        self.hostname = parsed_url.hostname
        self.id = self.hostname

        self.requests = []
        self.responses = []
        self.cookies = []

        self.screenshots = {}
        self.ocr_filename = "ocr/" + self.id

        self.cookie_notice_by_rules = []

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

    def set_cookies(self, cookies):
        self.cookies = cookies

    def add_screenshot(self, name, screenshot):
        self.screenshots[name] = screenshot

    def save_screenshots(self):
        for name in self.screenshots.keys():
            self.save_screenshot(name)

    def save_screenshot(self, name):
        with open(self.get_filename_for_screenshot(name), "wb") as file:
            file.write(base64.b64decode(self.screenshots[name]))

    def get_filename_for_screenshot(self, name):
        return "screenshots/" + self.id + "-" + name + ".png"

    def set_cookie_notice_by_rules(self, nodeIds):
        self.cookie_notice_by_rules = nodeIds


class Crawler:
    def __init__(self, debugger_url='http://127.0.0.1:9222'):
        # create a browser instance which controls chromium
        self.browser = pychrome.Browser(url=debugger_url)

        # create helpers
        self.abp_filter = AdBlockPlusFilter('resources/cookie-notice-css-rules.txt')

    def crawl_page(self, url):
        # initialize `_is_loaded` variable to `False`
        # it will be set to `True` when the `loadEventFired` event occurs
        self._is_loaded = False

        # create the result object
        self.result = WebpageResult(url=url)

        # create and setup a tab
        self.tab = self._setup_tab()
        
        # navigate to a specific page
        self.tab.Page.navigate(url=url, _timeout=15)
        
        # we wait for our load event to be fired (see `_event_load_event_fired`)
        while not self._is_loaded:
            self.tab.wait(0.1)

        # wait some time for events, after the page has been loaded to look
        # for further requests from JavaScript
        self.tab.wait(2)

        # get root node of document, is needed to be sure that the DOM is loaded
        root_node = self.tab.DOM.getDocument()

        #self.is_cmp_function_defined()
        #cookie_notices_node_ids, rules = self.find_cookie_notice_by_rules()

        cookie_string_node_ids = self.search_dom_for_cookie()
        fixed_parents = set([])
        for node_id in cookie_string_node_ids:
            fp_result = self.find_fixed_parent_of_node(node_id)
            if fp_result.get('has_fixed_parent'):
                fixed_parents.add(fp_result.get('fixed_parent'))

        #self.take_screenshots_of_visible_nodes(cookie_notices_node_ids, 'rules')
        self.take_screenshots_of_visible_nodes(cookie_string_node_ids, 'cookie-string')
        self.take_screenshots_of_visible_nodes(fixed_parents, 'fixed-parents')

        #self.take_screenshot('before')
        #self.tab.Input.emulateTouchFromMouseEvent(type="mouseWheel", x=1, y=1, button="none", deltaX=0, deltaY=-100)
        #self.tab.wait(0.1)
        #self.take_screenshot('after')

        # get cookies
        self.result.set_cookies(self.tab.Network.getAllCookies().get('cookies'))

        # stop and close the tab
        self._delete_all_cookies()
        self.tab.stop()
        self.browser.close_tab(self.tab)

        return self.result

    def _setup_tab(self):
        tab = self.browser.new_tab()

        # set callbacks for request and response logging
        tab.Network.requestWillBeSent = self._event_request_will_be_sent
        tab.Network.responseReceived = self._event_response_received
        tab.Page.loadEventFired = self._event_load_event_fired
        
        # start our tab after callbacks have been registered
        tab.start()
        
        # enable network notifications for all request/response so our
        # callbacks actually receive some data
        tab.Network.enable()

        # enable page domain notifications so our load_event_fired
        # callback is called when the page is loaded
        tab.Page.enable()

        # enable DOM, Runtime and Overlay
        tab.DOM.enable()
        tab.Runtime.enable()
        tab.Overlay.enable()

        return tab

    def _event_request_will_be_sent(self, request, **kwargs):
        """Will be called when a request is about to be sent.

        Those requests can still be blocked or intercepted and modified.
        This example script does not use any blocking or intercepting.

        Note: It does not say anything about the request being successful,
        there can still be connection issues.
        """
        url = request['url']
        self.result.add_request(request_url=url)

    def _event_response_received(self, response, **kwargs):
        """Will be called when a response is received.

        This includes the originating request which resulted in the
        response being received.
        """
        url = response['url']
        mime_type = response['mimeType']
        status = response['status']
        headers = response['headers']
        self.result.add_response(requested_url=url, status=status, mime_type=mime_type, headers=headers)

    def _event_load_event_fired(self, timestamp, **kwargs):
        """Will be called when the page sends an load event.

        Note that this only means that all resources are loaded, the
        page may still processes some JavaScript.
        """
        self._is_loaded = True

    def _highlight_node(self, node_id):
        """Highlight the given node with an overlay."""

        color_content = {'r': 152, 'g': 196, 'b': 234, 'a': 0.5}
        color_padding = {'r': 184, 'g': 226, 'b': 183, 'a': 0.5}
        highlightConfig = {'contentColor': color_content, 'paddingColor': color_padding}
        self.tab.Overlay.highlightNode(highlightConfig=highlightConfig, nodeId=node_id)

    def _hide_highlight(self):
        self.tab.Overlay.hideHighlight()

    def _delete_all_cookies(self):
        while(len(self.tab.Network.getAllCookies().get('cookies')) != 0):
            for cookie in self.tab.Network.getAllCookies().get('cookies'):
                self.tab.Network.deleteCookies(name=cookie.get('name'), domain=cookie.get('domain'), path=cookie.get('path'))

    def _get_node_id_for_remote_object_id(self, remote_object_id):
        return self.tab.DOM.requestNode(objectId=remote_object_id).get('nodeId')

    def _get_remote_object_id_for_node_id(self, node_id):
        return self.tab.DOM.resolveNode(nodeId=node_id).get('object').get('objectId')

    def search_dom_for_cookie(self):
        """Searches the DOM for the string `cookie` and returns all found nodes."""

        # stop execution of scripts to ensure that results do not change during search
        self.tab.Emulation.setScriptExecutionDisabled(value=True)

        # search for `cookie` in text
        search_object = self.tab.DOM.performSearch(query="//*[contains(translate(text(), 'COKIE', 'cokie'), 'cookie')]")
        node_ids = self.tab.DOM.getSearchResults(searchId=search_object.get('searchId'), fromIndex=0, toIndex=int(search_object.get('resultCount')))
        node_ids = node_ids['nodeIds']

        # resume execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=False)
        return node_ids

    def find_fixed_parent_of_node(self, node_id):
        js_function = """
            function findFixedParent(elem) {
                if (!elem) elem = this;
                while(elem && elem !== document) {
                    let style = getComputedStyle(elem);
                    if (style.position === 'fixed') {
                        return elem;
                    }
                    elem = elem.parentNode;
                }
                return false;
            }"""

        remote_object_id = self._get_remote_object_id_for_node_id(node_id)
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')

        # if a boolean is returned, the object is not visible
        if result.get('type') == 'boolean':
            return {
                'has_fixed_parent': False,
                'fixed_parent': None,
            }
        # otherwise, the object or one of its children is visible
        else:
            return {
                'has_fixed_parent': True,
                'fixed_parent': self._get_node_id_for_remote_object_id(result.get('objectId')),
            }

    def find_cookie_notice_by_rules(self):
        """Returns the node ids and the responsible rules of the found cookie notices.

        The function uses the AdblockPlus ruleset of the browser plugin `I DON'T CARE ABOUT COOKIES`.
        See: https://www.i-dont-care-about-cookies.eu/
        """
        node_ids = []
        rules = []
        root_node_id = self.tab.DOM.getDocument().get('root').get('nodeId')
        for rule in self.abp_filter.get_applicable_rules(self.result.hostname):
            found_node_ids = self.tab.DOM.querySelectorAll(nodeId=root_node_id, selector=rule.selector.get('value'))
            found_node_ids = found_node_ids['nodeIds']
            if len(found_node_ids) > 0:
                node_ids = node_ids + found_node_ids
                rules = rules + [rule * len(found_node_ids)]

        self.result.set_cookie_notice_by_rules(node_ids)
        return node_ids, rules

    def is_cmp_function_defined(self):
        """Checks whether the function `__cmp` is defined on the JavaScript `window` object."""

        result = self.tab.Runtime.evaluate(expression="typeof window.__cmp !== 'undefined'").get('result')
        return result.get('value')

    def find_clickables_in_node(self, node):
        pass
        #getEventListeners()
        # https://developers.google.com/web/tools/chrome-devtools/console/utilities?utm_campaign=2016q3&utm_medium=redirect&utm_source=dcc#geteventlistenersobject

    def is_node_visible(self, node_id):
        # Source: https://stackoverflow.com/a/41698614
        # adapted to also look at child nodes (especially important for fixed 
        # elements as their parents might not be visible themselves)
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
                const elemCenter   = {
                    x: elem.getBoundingClientRect().left + elem.offsetWidth / 2,
                    y: elem.getBoundingClientRect().top + elem.offsetHeight / 2
                };
                if (elemCenter.x < 0) visible = false;
                if (elemCenter.x > (document.documentElement.clientWidth || window.innerWidth)) visible = false;
                if (elemCenter.y < 0) visible = false;
                if (elemCenter.y > (document.documentElement.clientHeight || window.innerHeight)) visible = false;

                let pointContainer = document.elementFromPoint(elemCenter.x, elemCenter.y);
                do {
                    if (pointContainer === elem) return elem;
                    if (!pointContainer) break;
                } while (pointContainer = pointContainer.parentNode);

                // check the child nodes
                if (!visible) {
                    let childrenCount = elem.childNodes.length;
                    for (var i = 0; i < childrenCount; i++) {
                        let isChildVisible = isVisible(elem.childNodes[i]);
                        if (isChildVisible) {
                            return elem.childNodes[i];
                        }
                    }
                }

                return false;
            }"""

        # the function `isVisible` is calling itself recursively, 
        # therefore it needs to be defined beforehand
        self.tab.Runtime.evaluate(expression=js_function)

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

    def take_screenshots_of_visible_nodes(self, node_ids, name):
        # filter only visible nodes
        # and replace the original node_id with their visible children if the node itself is not visible
        node_ids = [visibility.get('visible_node') for visibility in (self.is_node_visible(node_id) for node_id in node_ids) if visibility.get('is_visible')]
        self.take_screenshots_of_nodes(node_ids, name)

    def take_screenshots_of_nodes(self, node_ids, name):
        # take a screenshot of the page with every node highlighted
        for index, node_id in enumerate(node_ids):
            self._highlight_node(node_id)
            self.take_screenshot(name + '-' + str(index))
            self._hide_highlight()

    def take_screenshot(self, name):
        # stop execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=True)

        # get the width and height of the viewport
        layout_metrics = self.tab.Page.getLayoutMetrics()
        viewport = layout_metrics.get('layoutViewport')
        width = viewport.get('clientWidth')
        height = viewport.get('clientHeight')
        x = viewport.get('pageX')
        y = viewport.get('pageY')
        screenshot_viewport = {"x": x, "y": y, "width": width, "height": height, "scale": 1}

        # take screenshot and store it
        self.result.add_screenshot(name, self.tab.Page.captureScreenshot(clip=screenshot_viewport)['data'])

        # resume execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=False)


class AdBlockPlusFilter:
    def __init__(self, rules_filename):
        with open(rules_filename) as filterlist:
            # we only need filters with type css
            # other instances are Header, Metadata, etc.
            # other type is url-pattern which is used to block script files
            self._rules = [rule for rule in parse_filterlist(filterlist) if isinstance(rule, abp.filters.parser.Filter) and rule.selector.get('type') == 'css']

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


def main():
    urls = []
    with open('resources/urls.txt') as f:
        pass
        urls = [line.strip() for line in f]

    c = Crawler()

    result_list = []
    for url in urls:
        result = c.crawl_page('https://' + url)
        result_list.append(result)

        result.save_screenshots()
        #subprocess.call(["tesseract", result.screenshot_filename, result.ocr_filename, "--oem", "1", "-l", "eng+deu"])


if __name__ == '__main__':
    main()
