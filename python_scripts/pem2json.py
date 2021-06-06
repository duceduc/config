#!/usr/bin/python3

import json
import os
import ssl
import sys
from collections import OrderedDict
from pprint import pprint as pp

def main():
    debug = False
    if len(sys.argv) == 3:
      if sys.argv[2] == "-d":
        debug = True

    if debug:
      print("Python {:s} on {:s}\n".format(sys.version, sys.platform))
      print("cli arg1: {:s}\n".format(sys.argv[1]))

    cert_file_name = os.path.join(os.path.dirname(__file__), sys.argv[1])
    try:
        ordered_dict = OrderedDict()
        ordered_dict = ssl._ssl._test_decode_cert(cert_file_name)
        if debug: pp(ordered_dict)

    except Exception as e:
        print("Error decoding certificate: {:s}\n".format(e))

    print(json.dumps(ordered_dict))

if __name__ == "__main__":
    main()
