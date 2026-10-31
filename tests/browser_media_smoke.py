"""Real local HTML audio; muted, no login, no external site or sound output."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
import wave
from assistant.browser import BrowserSession

audio=io.BytesIO()
with wave.open(audio,'wb') as wav:
    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\0'*16000)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data=audio.getvalue() if self.path.endswith('.wav') else b'<html><body><audio controls muted loop preload="auto" src="/silence.wav"></audio><button aria-label="Play" onclick="document.querySelector(\'audio\').play()">Play</button></body></html>'
        self.send_response(200)
        self.send_header('Content-Type','audio/wav' if self.path.endswith('.wav') else 'text/html')
        self.end_headers();self.wfile.write(data)
    def log_message(self,*_):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with tempfile.TemporaryDirectory(prefix='citadel-media-test-') as folder:
        browser=BrowserSession(profile=Path(folder)/'profile')
        try:
            snapshot=browser.call('navigate',url=f'http://127.0.0.1:{server.server_port}')
            button=next(item for item in snapshot['elements'] if item['text']=='Play')
            assert not browser.call('assess',requested_action='click',element_id=button['element_id'])['requires_confirmation']
            playing=browser.call('media',media_action='play')
            assert playing['media'][0]['paused'] is False and playing['media'][0]['ready_state']>=2,playing
            paused=browser.call('media',media_action='pause')
            assert paused['media'][0]['paused'] is True
            print('PASS: real muted HTML audio plays/pauses; media state verified, ordinary player control needs no approval')
        finally:
            browser.close()
finally:
    server.shutdown();server.server_close()
