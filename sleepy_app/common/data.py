# coding: utf-8

from sleepy_app.config import PROJECT_ROOT
import json
import os
import sleepy_app.common.responses as u
from jsonc_parser.parser import JsoncParser as jsonp
import tempfile


def initJson(filename="data.json"):
    try:
        jsonData = jsonp.parse_file(str(PROJECT_ROOT / 'example.jsonc'), encoding='utf-8')
        # 原子写入
        fd, tmp_path = tempfile.mkstemp(prefix='data_', suffix='.json', dir=os.path.dirname(os.path.abspath(filename)))
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(jsonData, file, indent=4, ensure_ascii=False)
        os.replace(tmp_path, filename)
    except:
        u.error('Create data.json failed')
        raise


class data:
    def __init__(self, filename="data.json"):
        self.filename = os.path.abspath(filename)
        os.makedirs(os.path.dirname(self.filename), exist_ok=True)
        if not os.path.exists(self.filename):
            u.warning('data.json not exist, creating')
            initJson(self.filename)
        with open(self.filename, 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def load(self):
        with open(self.filename, 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def save(self):
        # 原子写入到临时文件然后替换，避免写入中断造成损坏
        fd, tmp_path = tempfile.mkstemp(prefix='data_', suffix='.json', dir=os.path.dirname(self.filename))
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(self.data, file, indent=4, ensure_ascii=False)
        os.replace(tmp_path, self.filename)

    def dset(self, name, value):
        self.data[name] = value
        # 使用 save 做原子写入
        self.save()

    def dget(self, name):
        with open(self.filename, 'r', encoding='utf-8') as file:
            self.data = json.load(file)
            try:
                gotdata = self.data[name]
            except KeyError:
                gotdata = None
            return gotdata
