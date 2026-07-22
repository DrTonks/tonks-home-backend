# coding: utf-8

import json
import os
import utils as u
from jsonc_parser.parser import JsoncParser as jsonp
import tempfile


def initJson():
    try:
        jsonData = jsonp.parse_file('example.jsonc', encoding='utf-8')
        # 原子写入
        fd, tmp_path = tempfile.mkstemp(prefix='data_', suffix='.json', dir='.')
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(jsonData, file, indent=4, ensure_ascii=False)
        os.replace(tmp_path, 'data.json')
    except:
        u.error('Create data.json failed')
        raise


class data:
    def __init__(self):
        if not os.path.exists('data.json'):
            u.warning('data.json not exist, creating')
            initJson()
        with open('data.json', 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def load(self):
        with open('data.json', 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def save(self):
        # 原子写入到临时文件然后替换，避免写入中断造成损坏
        fd, tmp_path = tempfile.mkstemp(prefix='data_', suffix='.json', dir='.')
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(self.data, file, indent=4, ensure_ascii=False)
        os.replace(tmp_path, 'data.json')

    def dset(self, name, value):
        self.data[name] = value
        # 使用 save 做原子写入
        self.save()

    def dget(self, name):
        with open('data.json', 'r', encoding='utf-8') as file:
            self.data = json.load(file)
            try:
                gotdata = self.data[name]
            except KeyError:
                gotdata = None
            return gotdata
