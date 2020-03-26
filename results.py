#!/usr/bin/env python3

import argparse
import glob
import json
import os

from pprint import pprint

def filter_by_lambda(results, filter_function):
    return [result for result in results if filter_function(result)]

def filter_by_attribute(results, attribute):
    return [result for result in results if result.get(attribute)]

def filter_by_result_attribute(results, attribute):
    return [result for result in results if result.get('result').get(attribute)]

if __name__ == '__main__':
    ARG_TOP_2000 = '1'
    ARG_RANDOM = '2'

    # the dataset is passed to the script as argument
    # (top 2000 domains or random sampled domains)
    # default is top 2000 domains
    parser = argparse.ArgumentParser(description='Scans a list of domains, identifies cookie notices and evaluates them.')
    parser.add_argument('--dataset', dest='dataset', nargs='?', default='1',
                        help=f'the set of domains to scan: ' +
                             f'`{ARG_TOP_2000}` for the results in `results/top-2000` (dataset 1), ' +
                             f'`{ARG_RANDOM}` for the results in `results/random-2000` (dataset 2)')
    args = parser.parse_args()

    # load the correct dataset
    if args.dataset == ARG_TOP_2000:
        RESULTS_DIRECTORY = 'results/top-2000'
    else:
        RESULTS_DIRECTORY = 'results/random-2000'

    # get all relevant files
    files = []
    for file in os.listdir(RESULTS_DIRECTORY):
        if file.endswith(".json"):
            path = os.path.join(RESULTS_DIRECTORY, file)
            files.append(path)

    results = []
    for file in files:
        with open(file) as json_file:
            data = json.load(json_file)

            if data.get('failed'):
                continue

            # delete unnecessary data
            del data['requests']
            del data['responses']
            del data['html']

            # check which techniques detected a cookie notice
            data['result'] = dict()
            data['result']['cmp_defined'] = data.get('is_cmp_defined')
            data['result']['easylist-cookie'] = data.get('cookie_notice_count').get('easylist-cookie', 0) > 0
            data['result']['i-dont-care-about-cookies'] = data.get('cookie_notice_count').get('i-dont-care-about-cookies', 0) > 0
            data['result']['fixed_parent'] = data.get('cookie_notice_count').get('fixed_parent', 0) > 0
            data['result']['full_width_parent'] = data.get('cookie_notice_count').get('full_width_parent', 0) > 0
            data['result']['filters'] = data['result']['easylist-cookie'] or data['result']['i-dont-care-about-cookies']
            data['result']['own'] = data['result']['fixed_parent'] or data['result']['full_width_parent']

            data['result']['cmp_but_not_filters'] = data['result']['cmp_defined'] and not data['result']['filters']
            data['result']['cmp_but_not_own']     = data['result']['cmp_defined'] and not data['result']['own']
            data['result']['own_and_filters']     = data['result']['own'] and data['result']['filters']
            data['result']['own_but_not_filters'] = data['result']['own'] and not data['result']['filters']
            data['result']['filters_but_now_own'] = data['result']['filters'] and not data['result']['own']
            data['result']['easylist-cookie_but_now_own'] = data['result']['easylist-cookie'] and not data['result']['own']
            data['result']['i-dont-care-about-cookies-cookie_but_now_own'] = data['result']['i-dont-care-about-cookies'] and not data['result']['own']

            data['result']['third_party_cookie'] = False
            for cookie in data.get('cookies').get('all'):
                if data.get('domain') not in cookie.get('domain'):
                    data['result']['third_party_cookie'] = True

            results.append(data)

    detection_techniques = ['easylist-cookie', 'i-dont-care-about-cookies', 'fixed_parent', 'full_width_parent']

    total = 2000
    not_failed_count = len(results)
    failed_count = total - not_failed_count
    stopped_waiting_count = len(filter_by_attribute(results, 'stopped_waiting'))

    print('--- General data ---')
    print(f'total: {total}')
    print(f'failed_count: {failed_count}')
    print(f'stopped_waiting_count: {stopped_waiting_count}')
    print(f'not_failed_count: {not_failed_count}')
    print('')
    print('--- Detection techniques ---')
    for detection_technique, _ in results[0]['result'].items():
        print(f'{detection_technique}: {len(filter_by_result_attribute(results, detection_technique))}')

    """
    failed_skipped = filter_by_lambda(results, lambda x: x.get('failed') or x.get('skipped'))
    for fs in failed_skipped:
        fileList = list(glob.glob(f'{RESULTS_DIRECTORY}/{fs.get("rank")}-{fs.get("hostname")}*'))
        for filePath in fileList:
            try:
                os.remove(filePath)
            except:
                print("Error while deleting file : ", filePath)
    """
