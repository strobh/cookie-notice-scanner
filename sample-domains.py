#!/usr/bin/env python3

import random
from tranco import Tranco

if __name__ == '__main__':
    tranco = Tranco(cache=True, cache_dir='tranco')
    tranco_list = tranco.list(date='2020-03-01')
    all_domains = tranco_list.top()
    sampled_domains = random.sample(all_domains, 2000)

    with open('resources/sampled-domains.txt', 'w') as f:
        for domain in sampled_domains:
            f.write(f'{domain}\n')
