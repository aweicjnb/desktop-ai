"""Offline behavior checks for pinning, fullscreen and the consolidated UI."""
import gc
import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from window_layout import choose_monitor, clamp_rect, remember_rect, restore_rect


PRIMARY = dict(id='primary', bounds=(0, 0, 2880, 1800), work=(0, 0, 2880, 1704), primary=True)
SECONDARY = dict(id='secondary', bounds=(-1920, -400, 0, 680), work=(-1920, -400, 0, 640), primary=False)


class GeometryTests(unittest.TestCase):
    def test_pinned_monitor_ignores_pointer_on_other_screen(self):
        self.assertEqual(choose_monitor([PRIMARY, SECONDARY], 'secondary', (500, 500)), SECONDARY)

    def test_unpinned_window_follows_pointer(self):
        self.assertEqual(choose_monitor([PRIMARY, SECONDARY], None, (-1000, 100)), SECONDARY)

    def test_disconnected_monitor_falls_back_to_visible_screen(self):
        self.assertEqual(choose_monitor([PRIMARY], 'secondary', (-1000, 100)), PRIMARY)

    def test_dip_geometry_survives_mixed_dpi_and_negative_coordinates(self):
        saved = remember_rect((400, 400, 1020, 768), PRIMARY['work'], 2)
        rect = restore_rect(saved, SECONDARY['work'], 1)
        self.assertEqual(rect, (-1720, -200, 510, 384))

    def test_oversized_window_is_kept_in_work_area(self):
        self.assertEqual(clamp_rect((-9999, -9999, 9999, 9999), SECONDARY['work']), (-1920, -400, 1920, 1040))


class PopupWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from popup import Popup
        self.temporary = TemporaryDirectory(prefix='desktop-ai-window-test-')
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': self.temporary.name})
        self.environment.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = Popup(self.root, demo=True, smoke=True)
        self.root.attributes('-topmost', False)
        self.root.geometry('510x384+15000+15000')
        self.root.deiconify()
        self.root.update()

    def tearDown(self):
        self.app.exit()
        self.app = self.root = None
        gc.collect()
        self.environment.stop()
        self.temporary.cleanup()

    def test_fullscreen_restores_size_and_escape_restores_before_hiding(self):
        app = self.app
        # A physical monitor fixture avoids dependence on this machine's layout.
        screen = dict(id='test', bounds=(0, 0, 1600, 1000), work=(0, 0, 1600, 950), primary=True)
        app.put_rect((200, 150, 510, 384));self.root.update()
        with patch.object(app, 'current_monitor', return_value=screen), patch('popup.window_dpi', return_value=96):
            app.set_scale(1)
            app.toggle_fullscreen();self.root.update()
            self.assertTrue(app.fullscreen)
            self.assertFalse(app.resize.enabled)
            self.assertEqual(self.root.winfo_width(), 1600)
            self.assertEqual(self.root.winfo_height(), 1000)
            app.on_escape(None);self.root.update()
            self.assertFalse(app.fullscreen)
            self.assertTrue(app.resize.enabled)
            self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (510, 384))
            self.assertNotEqual(self.root.state(), 'withdrawn')

    def test_image_service_refusal_preserves_reply_and_shows_failure(self):
        app=self.app
        app.active='test-request';app.pending_prompt='make an image';app.started=True
        app.request_started=time.monotonic()
        with patch.object(app,'set_notice') as notice:
            app.accept({'id':'test-request','type':'complete','answer':'Original service reason',
                        'failed':True,'notice':'ChatGPT 未生成图片：结果可能与第三方内容过于相似。'})
        self.assertIsNone(app.active)
        self.assertEqual(app.answer,'Original service reason')
        self.assertEqual(app.status.cget('text'),'生成失败')
        notice.assert_called_once_with('ChatGPT 未生成图片：结果可能与第三方内容过于相似。')

    def test_pin_prevents_blur_hide_and_unpin_restores_it(self):
        app = self.app
        app.windows = SimpleNamespace(poll=lambda:None, foreground=lambda:False, close=lambda:None)
        app.pinned_device = 'test'
        app.tick()
        self.assertNotEqual(self.root.state(), 'withdrawn')
        app.pinned_device = None
        app.tick()
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_settings_restore_home_window_and_model_control_is_drawn(self):
        app = self.app
        before = self.root.winfo_width(), self.root.winfo_height()
        app.show_setup();self.root.update()
        self.assertEqual(app.settings, app.setup_panel)
        self.assertEqual(app.setup_panel.source_picker.winfo_class(), 'Canvas')
        self.assertEqual(app.setup_panel.model_picker.winfo_class(), 'Canvas')
        app.close_setup();self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), before)

    def test_custom_choice_supports_keyboard_selection(self):
        picker = self.app.setup_panel.model_picker
        picker.configure(values=['Auto', 'GPT-6'], descriptions=['自动选择', '快速回答'])
        picker.current(0)
        picker.open();self.root.update()
        picker.move(1)
        picker.choose(picker.row)
        self.assertEqual(picker.current(), 1)
        self.assertIsNone(picker.popup)

    def test_settings_closed_in_fullscreen_restore_home_size(self):
        app = self.app
        screen = dict(id='test', bounds=(0, 0, 1600, 1000), work=(0, 0, 1600, 950), primary=True)
        app.put_rect((200, 150, 510, 384));self.root.update()
        with patch.object(app, 'current_monitor', return_value=screen), patch('popup.window_dpi', return_value=96):
            app.set_scale(1)
            app.show_setup();self.root.update()
            app.toggle_fullscreen();self.root.update()
            app.close_setup();self.root.update()
            app.toggle_fullscreen();self.root.update()
            self.assertEqual(app.window_rect(), (200, 150, 510, 384))

    def test_settings_opened_in_fullscreen_restore_home_size(self):
        app = self.app
        screen = dict(id='test', bounds=(0, 0, 1600, 1000), work=(0, 0, 1600, 950), primary=True)
        app.put_rect((200, 150, 510, 384));self.root.update()
        with patch.object(app, 'current_monitor', return_value=screen), patch('popup.window_dpi', return_value=96):
            app.set_scale(1)
            app.toggle_fullscreen();self.root.update()
            app.show_setup();self.root.update()
            app.toggle_fullscreen();self.root.update()
            self.assertTrue(app.setup_panel.winfo_manager())
            self.assertGreaterEqual(self.root.winfo_height(), app.resize.min_height)
            app.close_setup();self.root.update()
            self.assertEqual(app.window_rect(), (200, 150, 510, 384))


if __name__ == '__main__':
    unittest.main(verbosity=2)
