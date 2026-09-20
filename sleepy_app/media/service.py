"""media/service business operations and HTTP handlers. State is application-scoped."""
import os
import sleepy_app.common.responses as u
from flask import redirect, request, send_from_directory
from werkzeug.utils import secure_filename
import uuid

class MediaService:
    def __init__(self, runtime):
        self.runtime = runtime

    def serve_image(self, filename):
        """提供博客项目/时光机图片"""
        if filename.startswith('projects/'):
            # Preserve the known legacy filename; other paths keep their exact case.
            filename = {'projects/jxj.JPG': 'projects/jxj.jpg'}.get(filename, filename)
            target = self.runtime.blog_service._blog_image_path('/images/' + filename)
            if not target:
                return self.runtime.common_security.reterr(code='not found', message='image not found'), 404
            response = redirect(target, code=302)
            response.headers['Cache-Control'] = 'public, max-age=300'
            return response
        # 安全检查：防止目录遍历
        safe_path = os.path.normpath(filename)
        if safe_path.startswith('..') or os.path.isabs(safe_path):
            return self.runtime.common_security.reterr(code='not found', message='image not found')
        file_path = os.path.join(self.runtime.IMAGES_DIR, safe_path)
        if not os.path.isfile(file_path):
            return self.runtime.common_security.reterr(code='not found', message=f'image not found: {filename}')
        # send_from_directory 需要正斜杠（Windows 兼容）
        return send_from_directory(self.runtime.IMAGES_DIR, safe_path.replace(os.sep, '/'), conditional=True)


    def music_list(self):
        """获取音乐文件列表（含 hasLyrics：是否有配对的 .lrc 歌词）"""
        self.runtime.d.load()
        music_files = self.runtime.d.data.get('music_files', [])
        result = []
        for m in music_files:
            item = dict(m)
            fname = os.path.basename(m.get('filename', ''))
            item['hasLyrics'] = bool(fname) and os.path.isfile(os.path.join(self.runtime.MUSIC_DIR, fname + '.lrc'))
            cover_name = os.path.basename(m.get('cover', ''))
            item['hasCover'] = bool(cover_name) and os.path.isfile(os.path.join(self.runtime.MUSIC_DIR, cover_name))
            item['coverUrl'] = f'/music/cover/{fname}' if item['hasCover'] else None
            item['coverVersion'] = os.stat(os.path.join(self.runtime.MUSIC_DIR, cover_name)).st_mtime_ns if item['hasCover'] else None
            result.append(item)
        return u.format_dict({'success': True, 'music': result})


    def music_stream(self, filename):
        """流媒体播放音乐文件"""
        # 安全检查：防止目录遍历
        safe_name = os.path.basename(filename)
        file_path = os.path.join(self.runtime.MUSIC_DIR, safe_name)

        if not os.path.isfile(file_path):
            return self.runtime.common_security.reterr(code='not found', message=f'music file not found: {safe_name}')

        ext = os.path.splitext(safe_name)[1].lower()
        mimetype_map = {
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.ogg': 'audio/ogg',
            '.flac': 'audio/flac',
            '.m4a': 'audio/mp4',
            '.aac': 'audio/aac',
        }
        mimetype = mimetype_map.get(ext, 'application/octet-stream')

        return send_from_directory(self.runtime.MUSIC_DIR, safe_name, mimetype=mimetype, conditional=True)


    def music_lyrics(self, filename):
        """获取音乐的 LRC 歌词（纯文本）；无配对 .lrc 则 not found（前端据此判定纯音乐）"""
        safe_name = os.path.basename(filename)
        lrc_name = safe_name + '.lrc'
        if not os.path.isfile(os.path.join(self.runtime.MUSIC_DIR, lrc_name)):
            return self.runtime.common_security.reterr(code='not found', message=f'lyrics not found: {safe_name}')
        return send_from_directory(self.runtime.MUSIC_DIR, lrc_name, mimetype='text/plain', conditional=True)


    def save_music_cover(self, cover_file, music_filename):
        """Validate and store one cover, returning its deterministic file name."""
        ext = os.path.splitext(cover_file.filename or '')[1].lower()
        if ext not in self.runtime.ALLOWED_COVER_EXTENSIONS:
            raise ValueError(
                f'unsupported cover type: {ext}. Allowed: {", ".join(sorted(self.runtime.ALLOWED_COVER_EXTENSIONS))}'
            )
        payload = cover_file.read(self.runtime.MAX_COVER_BYTES + 1)
        if len(payload) > self.runtime.MAX_COVER_BYTES:
            raise ValueError('cover image exceeds the 5 MB limit')
        if not payload:
            raise ValueError('cover image is empty')
        safe_music_name = os.path.basename(music_filename)
        cover_name = f'{safe_music_name}.cover{ext}'
        with open(os.path.join(self.runtime.MUSIC_DIR, cover_name), 'wb') as target:
            target.write(payload)
        return cover_name


    def remove_music_cover_files(self, music_filename, keep=None):
        """Remove covers for one track, optionally preserving the freshly written file."""
        prefix = f'{os.path.basename(music_filename)}.cover'
        for name in os.listdir(self.runtime.MUSIC_DIR):
            if name.startswith(prefix) and name != keep:
                try:
                    os.remove(os.path.join(self.runtime.MUSIC_DIR, name))
                except FileNotFoundError:
                    pass


    def music_cover(self, filename):
        """Serve the configured cover for one music file."""
        safe_name = os.path.basename(filename)
        self.runtime.d.load()
        entry = next((m for m in self.runtime.d.data.get('music_files', []) if m.get('filename') == safe_name), None)
        cover_name = os.path.basename(entry.get('cover', '')) if entry else ''
        if not cover_name or not os.path.isfile(os.path.join(self.runtime.MUSIC_DIR, cover_name)):
            return self.runtime.common_security.reterr(code='not found', message=f'cover not found: {safe_name}')
        return send_from_directory(self.runtime.MUSIC_DIR, cover_name, conditional=True)


    def music_cover_upload(self):
        """Add or replace the cover of an existing music file."""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err
        safe_name = os.path.basename(request.form.get('filename', ''))
        cover_file = request.files.get('cover')
        if not safe_name:
            return self.runtime.common_security.reterr(code='bad request', message='filename is required')
        if not cover_file or not cover_file.filename:
            return self.runtime.common_security.reterr(code='bad request', message='cover is required')

        with self.runtime.write_lock:
            self.runtime.d.load()
            music_files = self.runtime.d.data.get('music_files', [])
            entry = next((m for m in music_files if m.get('filename') == safe_name), None)
            if entry is None:
                return self.runtime.common_security.reterr(code='not found', message=f'music file not found in list: {safe_name}')
            try:
                cover_name = self.save_music_cover(cover_file, safe_name)
            except ValueError as exc:
                return self.runtime.common_security.reterr(code='bad request', message=str(exc))
            except Exception as exc:
                u.error(f'Cover save failed: {exc}')
                return self.runtime.common_security.reterr(code='server error', message='failed to save cover')
            self.remove_music_cover_files(safe_name, keep=cover_name)
            entry['cover'] = cover_name
            self.runtime.d.dset('music_files', music_files)

        return u.format_dict({
            'success': True,
            'code': 'OK',
            'filename': safe_name,
            'hasCover': True,
            'coverUrl': f'/music/cover/{safe_name}',
            'coverVersion': os.stat(os.path.join(self.runtime.MUSIC_DIR, cover_name)).st_mtime_ns,
        })


    def music_upload(self):
        """上传音乐文件（需管理员密钥）"""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err

        if 'file' not in request.files:
            return self.runtime.common_security.reterr(code='bad request', message='no file in request')

        file = request.files['file']
        if file.filename == '' or file.filename is None:
            return self.runtime.common_security.reterr(code='bad request', message='empty filename')

        # 检查扩展名
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in self.runtime.ALLOWED_MUSIC_EXTENSIONS:
            return self.runtime.common_security.reterr(code='bad request', message=f'unsupported file type: {ext}. Allowed: {", ".join(self.runtime.ALLOWED_MUSIC_EXTENSIONS)}')

        # 安全文件名
        original_name = secure_filename(file.filename)
        # 如重名，添加 uuid 前缀
        base, ext = os.path.splitext(original_name)
        save_name = f"{base}_{uuid.uuid4().hex[:8]}{ext}"

        try:
            file.save(os.path.join(self.runtime.MUSIC_DIR, save_name))
        except Exception as e:
            u.error(f'Music upload failed: {e}')
            return self.runtime.common_security.reterr(code='server error', message=f'failed to save file: {e}')

        # 从表单获取元数据
        title = request.form.get('title', '').strip() or base
        artist = request.form.get('artist', '').strip() or 'Unknown'

        cover_name = None
        cover_file = request.files.get('cover')
        if cover_file and cover_file.filename:
            try:
                cover_name = self.save_music_cover(cover_file, save_name)
            except ValueError as exc:
                try:
                    os.remove(os.path.join(self.runtime.MUSIC_DIR, save_name))
                except FileNotFoundError:
                    pass
                return self.runtime.common_security.reterr(code='bad request', message=str(exc))
            except Exception as exc:
                try:
                    os.remove(os.path.join(self.runtime.MUSIC_DIR, save_name))
                except FileNotFoundError:
                    pass
                u.error(f'Cover save failed: {exc}')
                return self.runtime.common_security.reterr(code='server error', message='failed to save cover')

        # 更新 data.json
        with self.runtime.write_lock:
            self.runtime.d.load()
            music_files = self.runtime.d.data.get('music_files', [])
            new_entry = {
                'filename': save_name,
                'title': title,
                'artist': artist
            }
            if cover_name:
                new_entry['cover'] = cover_name
            music_files.append(new_entry)
            self.runtime.d.dset('music_files', music_files)

        # 可选：随音乐一并上传的 LRC 歌词，存为 <音频文件名>.lrc（无则纯音乐）
        has_lyrics = False
        lyrics_file = request.files.get('lyrics')
        if lyrics_file and lyrics_file.filename:
            try:
                lyrics_file.save(os.path.join(self.runtime.MUSIC_DIR, save_name + '.lrc'))
                has_lyrics = True
            except Exception as e:
                u.error(f'Lyrics save failed: {e}')

        u.info(f'Music uploaded: {save_name} title="{title}" artist="{artist}" lyrics={has_lyrics}')
        return u.format_dict({
            'success': True,
            'code': 'OK',
            'file': {
                'filename': save_name,
                'title': title,
                'artist': artist,
                'hasLyrics': has_lyrics,
                'hasCover': bool(cover_name),
                'coverUrl': f'/music/cover/{save_name}' if cover_name else None,
                'coverVersion': os.stat(os.path.join(self.runtime.MUSIC_DIR, cover_name)).st_mtime_ns if cover_name else None,
            }
        })


    def music_delete(self):
        """删除音乐文件（需管理员密钥）"""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err

        try:
            body = request.get_json(force=True)
        except Exception:
            return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body')

        if body is None:
            return self.runtime.common_security.reterr(code='bad request', message='request body is required')

        try:
            if not isinstance(body, dict):
                return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')
            filename = body.get('filename', '')
        except AttributeError:
            return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')

        if not filename:
            return self.runtime.common_security.reterr(code='bad request', message='filename is required')

        safe_name = os.path.basename(filename)
        file_path = os.path.join(self.runtime.MUSIC_DIR, safe_name)

        # 从列表中移除
        with self.runtime.write_lock:
            self.runtime.d.load()
            music_files = self.runtime.d.data.get('music_files', [])
            new_list = [m for m in music_files if m.get('filename') != safe_name]

            if len(new_list) == len(music_files):
                # 没有找到对应的记录
                if os.path.isfile(file_path):
                    # 文件存在但列表中没有记录，仍然删除文件
                    os.remove(file_path)
                    u.info(f'Music file deleted (orphan): {safe_name}')
                    return u.format_dict({'success': True, 'code': 'OK', 'note': 'file was orphaned, removed from disk'})
                return self.runtime.common_security.reterr(code='not found', message=f'music file not found in list: {safe_name}')

            self.runtime.d.dset('music_files', new_list)

        # 删除物理文件
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            u.error(f'Failed to delete music file: {e}')

        # 联动删除配对的 .lrc 歌词
        try:
            os.remove(file_path + '.lrc')
        except FileNotFoundError:
            pass
        except Exception as e:
            u.error(f'Failed to delete lyrics file: {e}')

        self.remove_music_cover_files(safe_name)

        u.info(f'Music deleted: {safe_name}')
        return u.format_dict({'success': True, 'code': 'OK', 'deleted': safe_name})


    def music_reorder(self):
        """调整音乐播放顺序（需管理员密钥）：按传入的 filename 顺序重排 data.json 的 music_files"""
        auth_err = self.runtime.common_security.require_admin()
        if auth_err:
            return auth_err

        try:
            body = request.get_json(force=True)
        except Exception:
            return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body')

        if not isinstance(body, dict):
            return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')

        order = body.get('order', [])
        if not isinstance(order, list):
            return self.runtime.common_security.reterr(code='bad request', message='order must be a list of filenames')

        with self.runtime.write_lock:
            self.runtime.d.load()
            music_files = self.runtime.d.data.get('music_files', [])
            by_name = {m.get('filename'): m for m in music_files}
            new_list = []
            added = set()
            for f in order:
                if f in by_name and f not in added:
                    new_list.append(by_name[f])
                    added.add(f)  # 去重：order 内重复的 filename 只取一次
            # 补上 order 未包含的条目（防止漏传导致丢歌）
            for m in music_files:
                if m.get('filename') not in added:
                    new_list.append(m)
            self.runtime.d.dset('music_files', new_list)

        u.info(f'Music reordered: {len(new_list)} tracks')
        return u.format_dict({'success': True, 'code': 'OK'})

