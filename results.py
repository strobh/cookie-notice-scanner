#!/usr/bin/env python3

import argparse
import glob
import json
import os
import re
import string
from collections import Counter

import nltk
from pprint import pprint
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

nltk.download('punkt')
nltk.download('stopwords')


def filter_by_lambda(results, filter_function):
    return [result for result in results if filter_function(result)]

def filter_by_attribute(results, attribute):
    return [result for result in results if result.get(attribute)]

def filter_by_detection_attribute(results, attribute):
    return [result for result in results if result.get('detection').get(attribute)]

def is_notice_part_of_other(cookie_notice1, cookie_notice2):
    return cookie_notice1.get('html') in cookie_notice2.get('html') and cookie_notice1.get('html') != cookie_notice2.get('html')

def is_notice_equal(cookie_notice1, cookie_notice2):
    return cookie_notice1.get('html') == cookie_notice2.get('html')

def is_notice_part_of_some_other(index, cookie_notice, cookie_notices):
    for i, cookie_notice_other in enumerate(data['cookie_notices']['own']):
        if is_notice_part_of_other(cookie_notice, cookie_notice_other):
            cookie_notice_other['techniques'].extend(cookie_notice['techniques'])
            return True
        if is_notice_equal(cookie_notice, cookie_notice_other) and index > i:
            cookie_notice_other['techniques'].extend(cookie_notice['techniques'])
            return True
    return False

def count_words(string):
    return len(re.findall(r'\w+', string))


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

    word_counts = []
    words = {}

    cnt = 0
    results = []
    for file in files:
        #if cnt > 50:
        #    continue
        cnt += 1
        with open(file) as json_file:
            data = json.load(json_file)

            if data.get('failed'):
                continue

            # delete unnecessary data
            del data['requests']
            del data['responses']
            del data['html']

            language = data.get('language', 'none')

            # check which techniques detected a cookie notice
            data['detection'] = dict()
            data['detection']['cmp_defined'] = data.get('is_cmp_defined')
            data['detection']['easylist-cookie'] = data.get('cookie_notice_count').get('easylist-cookie', 0) > 0
            data['detection']['i-dont-care-about-cookies'] = data.get('cookie_notice_count').get('i-dont-care-about-cookies', 0) > 0
            data['detection']['fixed_parent'] = data.get('cookie_notice_count').get('fixed_parent', 0) > 0
            data['detection']['full_width_parent'] = data.get('cookie_notice_count').get('full_width_parent', 0) > 0
            data['detection']['filters'] = data['detection']['easylist-cookie'] or data['detection']['i-dont-care-about-cookies']
            data['detection']['own'] = data['detection']['fixed_parent'] or data['detection']['full_width_parent']

            data['detection']['cmp_but_not_filters'] = data['detection']['cmp_defined'] and not data['detection']['filters']
            data['detection']['cmp_but_not_own'] = data['detection']['cmp_defined'] and not data['detection']['own']
            data['detection']['own_and_filters'] = data['detection']['own'] and data['detection']['filters']
            data['detection']['own_but_not_filters'] = data['detection']['own'] and not data['detection']['filters']
            data['detection']['filters_but_now_own'] = data['detection']['filters'] and not data['detection']['own']
            data['detection']['easylist-cookie_but_now_own'] = data['detection']['easylist-cookie'] and not data['detection']['own']
            data['detection']['i-dont-care-about-cookies-cookie_but_now_own'] = data['detection']['i-dont-care-about-cookies'] and not data['detection']['own']

            data['detection']['third_party_cookie'] = False
            for cookie in data.get('cookies').get('all'):
                if data.get('domain') not in cookie.get('domain'):
                    data['detection']['third_party_cookie'] = True

            # merge fixed and full-width into common list
            own_cookie_notices = []
            for detection_technique, cookie_notices in data.get('cookie_notices').items():
                if detection_technique != 'fixed_parent' and detection_technique != 'full_width_parent':
                    continue
                for cookie_notice in cookie_notices:
                    cookie_notice['techniques'] = [detection_technique]
                    own_cookie_notices.append(cookie_notice)
            data['cookie_notices']['own'] = own_cookie_notices

            # filter cookie notices that are part of another (needed only once)
            data['cookie_notices']['own'] = [
                    cookie_notice
                    for index, cookie_notice in enumerate(data['cookie_notices']['own'])
                    if not is_notice_part_of_some_other(index, cookie_notice, data['cookie_notices']['own'])]

            # other results
            data['result'] = dict()
            if len(data['cookie_notices']['own']) == 1:
                cookie_notice = data['cookie_notices']['own'][0]

                # get text of cookie notice with spaces
                cn_text = cookie_notice.get('text')
                cn_text = cn_text.replace("\r","")
                cn_text = cn_text.replace("\n","")
                cn_text = cn_text.replace(".",". ")

                data['result']['word_count'] = count_words(cn_text)

                word_counts.append(data['result']['word_count'])
                if language not in words:
                    words[language] = []
                words[language].append(cn_text)

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
    for detection_technique, _ in results[0]['detection'].items():
        print(f'{detection_technique}: {len(filter_by_detection_attribute(results, detection_technique))}')

    print('')
    print('--- Results ---')
    print(f'avg word count: {sum(word_counts) / len(word_counts)}')
    print('most common words in english')
    combined_words = ' '.join(words['en']).lower()
    combined_words = combined_words.translate(str.maketrans('', '', string.punctuation))
    #words_tokenized = word_tokenize(combined_words)
    words_tokenized = combined_words.split()
    words_tokenized = [word for word in words_tokenized if not word in stopwords.words('english')]
    Counter = Counter(words_tokenized)
    most_occur = Counter.most_common(100)
    print(most_occur)

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
