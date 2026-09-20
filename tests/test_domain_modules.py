import importlib
import unittest
from pathlib import Path
from sleepy_app.community.avatars import community_avatar_svg, community_qq_number
from sleepy_app.status.history import append_status_history
from sleepy_app.status.heatmap import calc_heatmap_intensity

class DomainModuleTests(unittest.TestCase):
    def test_avatar_normalization_and_numeric_qq_only(self):
        self.assertEqual(community_avatar_svg(' A@EXAMPLE.COM '), community_avatar_svg('a@example.com'))
        self.assertEqual(community_qq_number('123456@qq.com'), '123456')
        self.assertIsNone(community_qq_number('name@qq.com'))

    def test_history_coalesces_and_caps_at_five(self):
        data = {}
        for i in range(7): append_status_history(data, i, str(i), i)
        append_status_history(data, 6, '6', 99)
        self.assertEqual(len(data['status_history']), 5)
        self.assertEqual(data['status_history'][-1]['timestamp'], 99)

    def test_heatmap_preserves_input(self):
        original = [{'messageCount': 0}, {'messageCount': 4}]
        result = calc_heatmap_intensity(original)
        self.assertEqual(result[0]['intensity'], 0)
        self.assertNotIn('intensity', original[0])
