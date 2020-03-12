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
        self.tab.wait(1)

        # get root node of document, is needed to be sure that the DOM is loaded
        root_node = self.tab.DOM.getDocument()

        cookie_notices_node_ids, rules = self.find_cookie_notice_by_rules()
        print(rules)
        cookie_notices_node_ids = [node_id for node_id in cookie_notices_node_ids if self.is_node_visible(node_id)]
        #self.is_cmp_function_defined()

        #self.search_dom_for_cookie()

        #self.take_screenshot('before')
        #self.tab.Input.emulateTouchFromMouseEvent(type="mouseWheel", x=1, y=1, button="none", deltaX=0, deltaY=-100)
        #self.tab.wait(0.1)
        #self.take_screenshot('after')

        color_content = {'r': 152, 'g': 196, 'b': 234, 'a': 0.5}
        color_padding = {'r': 184, 'g': 226, 'b': 183, 'a': 0.5}
        hightlightConfig = {'contentColor': color_content, 'paddingColor': color_padding}
        for index, node_id in enumerate(cookie_notices_node_ids):
            self.tab.Overlay.highlightNode(highlightConfig=hightlightConfig, nodeId=node_id)
            self.take_screenshot('overlay-' + str(index))

        # stop and close the tab
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

        # enable DOM and Runtime
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
        #pprint(request)
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

    def take_screenshot(self, name):
        # get the width and height of the viewport
        layout_metrics = self.tab.Page.getLayoutMetrics()
        viewport = layout_metrics.get('layoutViewport')
        width = viewport.get('clientWidth')
        height = viewport.get('clientHeight')
        x = viewport.get('pageX')
        y = viewport.get('pageY')
        screenshot_viewport = {"x": x, "y": y, "width": width, "height": height, "scale": 2}

        # take screenshot and store it
        self.result.add_screenshot(name, self.tab.Page.captureScreenshot(clip=screenshot_viewport)['data'])

    def search_dom_for_cookie(self):
        # stop execution of scripts to ensure that results do not change during search
        self.tab.Emulation.setScriptExecutionDisabled(value=True)

        # search for `cookie` in text
        search_object = self.tab.DOM.performSearch(query="//*[contains(translate(text(), 'COKIE', 'cokie'), 'cookie')]")
        node_ids = self.tab.DOM.getSearchResults(searchId=search_object.get('searchId'), fromIndex=0, toIndex=int(search_object.get('resultCount')))
        node_ids = node_ids['nodeIds']

        # resume execution of scripts
        self.tab.Emulation.setScriptExecutionDisabled(value=False)
        return node_ids

    def find_cookie_notice_by_rules(self):
        results = []
        rules = []
        root_node_id = self.tab.DOM.getDocument().get('root').get('nodeId')
        for rule in self.abp_filter._rules:
            if self.abp_filter._is_rule_applicable(rule, self.result.hostname):
                node_ids = self.tab.DOM.querySelectorAll(nodeId=root_node_id, selector=rule.selector.get('value'))
                node_ids = node_ids['nodeIds']
                if len(node_ids) > 0:
                    results = results + node_ids
                    rules = rules + [rule * len(node_ids)]

        self.result.set_cookie_notice_by_rules(results)
        return results, rules

    def is_cmp_function_defined(self):
        result = self.tab.Runtime.evaluate(expression="typeof window.__cmp !== 'undefined'").get('result')
        return result.get('value')

    def find_clickables_in_node(self, node):
        pass
        #getEventListeners()
        # https://developers.google.com/web/tools/chrome-devtools/console/utilities?utm_campaign=2016q3&utm_medium=redirect&utm_source=dcc#geteventlistenersobject

    def is_node_visible(self, node_id):
        # Source: https://stackoverflow.com/a/41698614
        js_function = """
            function() {
                elem = this;
                if (!(elem instanceof Element)) throw Error('DomUtil: elem is not an element.');
                const style = getComputedStyle(elem);
                if (style.display === 'none') return false;
                if (style.visibility !== 'visible') return false;
                if (style.opacity < 0.1) return false;
                if (elem.offsetWidth + elem.offsetHeight + elem.getBoundingClientRect().height +
                    elem.getBoundingClientRect().width === 0) {
                    return false;
                }
                const elemCenter   = {
                    x: elem.getBoundingClientRect().left + elem.offsetWidth / 2,
                    y: elem.getBoundingClientRect().top + elem.offsetHeight / 2
                };
                if (elemCenter.x < 0) return false;
                if (elemCenter.x > (document.documentElement.clientWidth || window.innerWidth)) return false;
                if (elemCenter.y < 0) return false;
                if (elemCenter.y > (document.documentElement.clientHeight || window.innerHeight)) return false;
                let pointContainer = document.elementFromPoint(elemCenter.x, elemCenter.y);
                do {
                    if (pointContainer === elem) return true;
                    if (!pointCointainer) return false;
                } while (pointContainer = pointContainer.parentNode);
                return false;
            }"""

        remote_object_id = self.tab.DOM.resolveNode(nodeId=node_id).get('object').get('objectId')
        result = self.tab.Runtime.callFunctionOn(functionDeclaration=js_function, objectId=remote_object_id, silent=True).get('result')
        return result.get('value')


class AdBlockPlusFilter:
    def __init__(self, rules_filename):
        with open(rules_filename) as filterlist:
            # we only need filters with type css
            # other instances are Header, Metadata, etc.
            # other type is url-pattern which is used to block script files
            self._rules = [rule for rule in parse_filterlist(filterlist) if isinstance(rule, abp.filters.parser.Filter) and rule.selector.get('type') == 'css']

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
