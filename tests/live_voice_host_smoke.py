"""Actual resident models warm before commands, with networking disabled in child."""
import queue
import time
from assistant.session import SessionProcess

events = queue.Queue()
host = SessionProcess(lambda sid, event, data: events.put((event, data)), module="assistant.voice_host")
def receive(kind, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        event, payload = events.get(timeout=max(.01, deadline-time.monotonic()))
        print('VOICE EVENT:', event, str(payload)[:180], flush=True)
        if event == 'error':
            raise AssertionError(payload)
        if event == kind:
            return payload
    raise AssertionError('Voice host timeout: '+kind)

try:
    host.start({'speech': True})
    ready = receive('ready',120)
    assert ready['speech']
    host.send({'command':'cancel','token':'cancel-one'})
    host.send({'command':'inspect'})
    one = receive('state',10)
    host.send({'command':'cancel','token':'cancel-two'})
    host.send({'command':'inspect'})
    two = receive('state',10)
    assert one == two == {key:ready[key] for key in ('stt_instance','tts_instance')}
    assert host.process.poll() is None
    print('PASS: real Whisper and TTS warm before requests, survive two cancellations in the same offline resident process')
finally:
    host.stop()
    if host.process:
        host.process.wait(timeout=10)
