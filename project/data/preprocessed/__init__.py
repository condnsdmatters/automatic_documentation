from collections import namedtuple
import os

from yaml import load, CLoader
from yaml.constructor import Constructor


## Deal with Yaml 1.2 and 1.1 incompatibilty: Turn off 'on' == True (bool)
def add_bool(self, node):
    return self.construct_scalar(node)
Constructor.add_constructor(u'tag:yaml.org,2002:bool', add_bool)

Data = namedtuple("Data", ["train","test"])

def load_main():
    dirname = os.path.dirname(os.path.abspath(__file__))
    train_file = '{}/main_train.yaml'.format(dirname)
    test_file = '{}/main_test.yaml'.format(dirname)

    if os.path.isfile(train_file) and  os.path.isfile(test_file):
        with open(train_file, 'r') as f:
            train = load(f, Loader=CLoader)
        with open(test_file, 'r') as f:
            test = load(f, Loader=CLoader)
        return Data(train, test)
    else:
        return None



data = load_main()
