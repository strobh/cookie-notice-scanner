# Cookie Notice Scanner

This tool was developed as part of a bachelor thesis to automatically recognize cookie notices on websites.


## Prerequisites

- Chromium (or Google Chrome) browser
- Python3


## Install dependencies

```
$ pipenv install
```


## Run the script

First, run the browser in automation mode using the debugging port `9222`. For Mac users:

```
$ ./run-chromium.sh
```

Then, run the script `scan.py`:

```
$ pipenv run python scan.py
```


## Help

The script `scan.py` has multiple options including a help option:

```
$ pipenv run python scan.py --help
usage: scan.py [-h] [--dataset [DATASET]] [--results [RESULTS_DIRECTORY]]
               [--click]

Scans a list of domains, identifies cookie notices and evaluates them.

optional arguments:
  -h, --help            show this help message and exit
  --dataset [DATASET]   the set of domains to scan: `1` for the top 2000
                        domains, `2` for domains in file `resources/sampled-
                        domains.txt`
  --results [RESULTS_DIRECTORY]
                        the directory to store the the results in (default:
                        `results`)
  --click               whether all links and buttons in the detected cookie
                        notices should be clicked or not (default: false)
```
