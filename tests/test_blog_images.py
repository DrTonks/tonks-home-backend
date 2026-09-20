import unittest
from sleepy_app.blog.images import blog_image_url, normalize_blog_images


class BlogImagesTests(unittest.TestCase):
    def test_no_local_copy_and_field_shapes(self):
        original = {'image': '/images/projects/new.JPG', 'title': 'new'}
        result = normalize_blog_images(original, 'https://blog.test')
        self.assertEqual(result['image'], 'https://blog.test/images/projects/new.JPG')
        self.assertEqual(result['images'], [result['image']])
        self.assertEqual(original['image'], '/images/projects/new.JPG')
        timeline = normalize_blog_images({'image': [original['image']]}, 'https://blog.test')
        self.assertEqual(timeline['image'], timeline['images'])

    def test_encoding_and_idempotence(self):
        url = blog_image_url('/images/projects/照片 1.png', 'https://blog.test')
        self.assertEqual(url, 'https://blog.test/images/projects/%E7%85%A7%E7%89%87%201.png')
        self.assertEqual(blog_image_url(url, 'https://blog.test'), url)

    def test_untrusted_paths(self):
        for value in ['//evil.test/x', 'https://evil.test/images/x', '/images/../secret',
                      '/images/%2e%2e/x', '/images/%252e%252e/x', '/images/a\\x',
                      '/images/x?url=https://evil.test', '/images//x', '/images/x\x00', None]:
            with self.subTest(value=value):
                self.assertIsNone(blog_image_url(value, 'https://blog.test'))

    def test_invalid_configuration(self):
        for base in ['javascript:bad', 'https://user:pass@blog.test', '', None]:
            self.assertIsNone(blog_image_url('/images/x.png', base))
