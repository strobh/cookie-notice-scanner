import glob
import json
import os
from pprint import pprint

def filter_by_lambda(results, filter_function):
    return [result for result in results if filter_function(result)]

if __name__ == '__main__':
    RESULTS_DIRECTORY = 'results-block'

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

            results.append({
                'rank': data.get('rank'),
                'url': data.get('url'),
                'hostname': data.get('hostname'),
                'failed': data.get('failed'),
                'skipped': data.get('skipped'),
                'language': data.get('language'),
                'is_cmp_defined': data.get('is_cmp_defined'),
                'rules': data.get('cookie_notice_count').get('rules', 0) > 0,
                'fixed_parent': data.get('cookie_notice_count').get('fixed-parent', 0) > 0,
                'full_width_parent': data.get('cookie_notice_count').get('full-width-parent', 0) > 0,
            })

    total = len(results)
    failed_skipped_count = len(filter_by_lambda(results, lambda x: x.get('failed') or x.get('skipped')))
    not_failed_or_skipped_count = total-failed_skipped_count
    cmp_defined_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('is_cmp_defined')))
    rules_detection_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('rules')))
    own_detection_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('fixed_parent') or x.get('full_width_parent')))
    fixed_parent_detection_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('fixed_parent')))
    full_width_parent_detection_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('full_width_parent')))
    cmp_but_not_own_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('is_cmp_defined') and not x.get('fixed_parent') and not x.get('full_width_parent')))
    cmp_but_not_rules_count = len(filter_by_lambda(results, lambda x: not x.get('failed') and x.get('is_cmp_defined') and not x.get('rules')))

    print(f'total: {total}')
    print(f'failed_skipped_count: {failed_skipped_count}')
    print(f'not_failed_or_skipped_count: {not_failed_or_skipped_count}')
    print(f'cmp_defined_count: {cmp_defined_count}')
    print(f'rules_detection_count: {rules_detection_count}')
    print(f'own_detection_count: {own_detection_count}')
    print(f'fixed_parent_detection_count: {fixed_parent_detection_count}')
    print(f'full_width_parent_detection_count: {full_width_parent_detection_count}')
    print(f'cmp_but_not_own_count: {cmp_but_not_own_count}')
    print(f'cmp_but_not_rules_count: {cmp_but_not_rules_count}')

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
