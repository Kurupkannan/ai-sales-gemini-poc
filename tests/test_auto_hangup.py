"""The agent ends the call itself, and calls spread across the caller-number pool."""
from types import SimpleNamespace
from uuid import uuid4

from app.config import Settings
from app.gemini.agent_prompt import call_prompt
from app.gemini.live_session import END_CALL_TOOL, build_live_connect_config
from app.telephony import preroll


class FakeSession:
    def __init__(self, messages):
        self.messages = messages

    async def receive(self):
        for message in self.messages:
            yield message
        self.messages = []


def test_live_config_offers_end_call_and_prompt_explains_it():
    config = build_live_connect_config('base prompt')
    names = [f.name for tool in config.tools for f in tool.function_declarations]
    assert names == [END_CALL_TOOL]
    assert 'end_call' in call_prompt('base', 'Asha', None, None)


async def test_end_call_queues_checkpoint_after_goodbye_audio():
    audio = preroll.PreparedAudio()
    audio.attached = True
    audio.stream_id = 'stream-1'
    goodbye = SimpleNamespace(tool_call=None, go_away=None, server_content=SimpleNamespace(
        interrupted=False, turn_complete=False, input_transcription=None,
        output_transcription=SimpleNamespace(text='നന്ദി, നമസ്കാരം!'),
        model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b'\0\0' * 2400))])))
    hangup = SimpleNamespace(go_away=None, server_content=None, tool_call=SimpleNamespace(
        function_calls=[SimpleNamespace(name=END_CALL_TOOL, args={'reason': 'completed'})]))
    await preroll._pump(SimpleNamespace(id=uuid4()), audio, FakeSession([goodbye, hangup]))
    queued = [audio.queue.get_nowait()[1] for _ in range(audio.queue.qsize())]
    assert audio.hangup.is_set()
    assert queued[0]['event'] == 'playAudio'
    assert queued[-1] == {'event': 'checkpoint', 'streamId': 'stream-1', 'name': preroll.HANGUP_CHECKPOINT}


def test_caller_number_pool_sets_capacity():
    config = Settings(_env_file=None, PLIVO_FROM_NUMBERS='+918000000001, +918000000002,+918000000001',
                      PLIVO_FROM_NUMBER='+918000000009', TELEPHONY_CALLS_PER_NUMBER=2)
    assert config.from_numbers == ['+918000000001', '+918000000002']
    assert config.call_capacity == 4
    assert Settings(_env_file=None, PLIVO_FROM_NUMBER='+918000000009').from_numbers == ['+918000000009']


async def test_silent_line_gets_one_check_then_close_then_hangup(monkeypatch):
    import time
    from app.gemini.agent_prompt import SILENCE_CHECK, SILENCE_CLOSE
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_SILENCE_SECONDS', 0.05)
    sent = []
    async def send_instruction(session, text):
        sent.append(text)
    audio = preroll.PreparedAudio()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    audio.last_heard = time.monotonic()
    await bridge.watch_silence(audio, SimpleNamespace(id=uuid4()), tick=0.02)
    assert sent == [SILENCE_CHECK, SILENCE_CLOSE]


async def test_caller_speech_resets_the_silence_count(monkeypatch):
    import asyncio, time
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_SILENCE_SECONDS', 0.05)
    sent = []
    audio = preroll.PreparedAudio()
    async def send_instruction(session, text):
        sent.append(text)
        if len(sent) == 1:
            audio.caller_turns += 1  # They answer the first check.
            audio.last_heard = time.monotonic()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    await asyncio.wait_for(bridge.watch_silence(audio, SimpleNamespace(id=uuid4()), tick=0.02), 2)
    assert len(sent) == 3  # check, (reset by speech) check, close


async def test_long_call_is_told_to_close_then_hung_up():
    from app.gemini.agent_prompt import WRAP_UP
    from app.telephony import bridge
    sent = []
    async def send_instruction(session, text):
        sent.append(text)
    audio = preroll.PreparedAudio()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    await bridge.wrap_up_after(audio, SimpleNamespace(id=uuid4()), limit=0.01, grace=0.05)
    assert sent == [WRAP_UP]  # Returned: the bridge hangs up because the agent never closed.


async def test_agent_closing_in_time_leaves_hangup_to_the_goodbye():
    import asyncio
    from app.telephony import bridge
    audio = preroll.PreparedAudio()
    async def send_instruction(session, text):
        audio.hangup.set()  # Agent says goodbye and calls end_call.
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    task = asyncio.create_task(bridge.wrap_up_after(audio, SimpleNamespace(id=uuid4()), limit=0.01, grace=1))
    await asyncio.sleep(0.1)
    assert not task.done()  # Waits for the goodbye to finish playing instead of cutting it off.
    task.cancel()


def test_script_keeps_off_topic_turns_short():
    from app.gemini.agent_prompt import SYSTEM_INSTRUCTION
    assert 'OFF-TOPIC' in SYSTEM_INSTRUCTION and 'at most two questions' in SYSTEM_INSTRUCTION
    assert 'never give tips' in SYSTEM_INSTRUCTION


def _agent_line(text):
    return SimpleNamespace(tool_call=None, go_away=None, server_content=SimpleNamespace(
        interrupted=False, turn_complete=False, input_transcription=None,
        output_transcription=SimpleNamespace(text=text), model_turn=None))


def _end_call():
    return SimpleNamespace(go_away=None, server_content=None, tool_call=SimpleNamespace(
        function_calls=[SimpleNamespace(id='fc-1', name=END_CALL_TOOL, args={})]))


async def test_hangup_right_after_a_question_is_refused():
    refused = []
    async def send_tool_result(session, call_id, name, result):
        refused.append(call_id)
    audio = preroll.PreparedAudio()
    audio.attached, audio.stream_id = True, 'stream-1'
    audio.manager = SimpleNamespace(send_tool_result=send_tool_result)
    await preroll._pump(SimpleNamespace(id=uuid4()), audio, FakeSession([_agent_line('ജോയിൻ ചെയ്യാമോ?'), _end_call()]))
    assert refused == ['fc-1'] and not audio.hangup.is_set()


async def test_hangup_after_the_closing_line_is_accepted():
    from app.gemini.agent_prompt import NO_REPLY
    audio = preroll.PreparedAudio()
    audio.attached, audio.stream_id = True, 'stream-1'
    await preroll._pump(SimpleNamespace(id=uuid4()), audio, FakeSession([_agent_line(NO_REPLY), _end_call()]))
    assert audio.hangup.is_set()


async def test_silence_counts_from_pickup_not_from_ringing(monkeypatch):
    import asyncio, time
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_SILENCE_SECONDS', 0.2)
    sent = []
    async def send_instruction(session, text):
        sent.append(text)
    audio = preroll.PreparedAudio()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    audio.last_heard = time.monotonic() - 20  # Greeting prepared 20 s before pickup.
    task = asyncio.create_task(bridge.watch_silence(audio, SimpleNamespace(id=uuid4()), tick=0.02))
    await asyncio.sleep(0.1)
    assert sent == []  # No check-in the moment they pick up.
    task.cancel()


def _caller_line(text):
    return SimpleNamespace(tool_call=None, go_away=None, server_content=SimpleNamespace(
        interrupted=False, turn_complete=False, input_transcription=SimpleNamespace(text=text),
        output_transcription=None, model_turn=None))


async def test_agent_is_nudged_when_it_does_not_answer_hello(monkeypatch):
    import asyncio, time
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_REPLY_NUDGE_SECONDS', 0.05)
    monkeypatch.setattr(bridge.settings, 'CALL_SILENCE_SECONDS', 30)
    sent = []
    async def send_instruction(session, text):
        sent.append(text)
    audio = preroll.PreparedAudio()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    audio.caller_said, audio.caller_turns, audio.caller_spoke_at = 'Hello.', 1, time.monotonic()
    task = asyncio.create_task(bridge.watch_silence(audio, SimpleNamespace(id=uuid4()), tick=0.02))
    await asyncio.sleep(0.3)
    task.cancel()
    assert len(sent) == 1 and "'Hello.'" in sent[0]  # Nudged once, not repeatedly.


async def test_interrupting_the_goodbye_cancels_the_hangup():
    from app.gemini.agent_prompt import YES_REPLY
    results = []
    async def send_tool_result(session, call_id, name, result):
        results.append((call_id, result['result'].split(':')[0]))
    audio = preroll.PreparedAudio()
    audio.attached, audio.stream_id = True, 'stream-1'
    audio.manager = SimpleNamespace(send_tool_result=send_tool_result)
    call = SimpleNamespace(id=uuid4(), transcript=[], add_transcript=lambda s, t: call.transcript.append((s, t)))
    await preroll._pump(call, audio, FakeSession([_agent_line(YES_REPLY), _end_call(), _caller_line('അല്ല, വേണ്ട')]))
    assert not audio.hangup.is_set()
    assert results == [('fc-1', 'cancelled')]
    queued = [audio.queue.get_nowait()[1]['event'] for _ in range(audio.queue.qsize())]
    assert queued[-1] == 'clearAudio'  # The rest of the goodbye stops playing.


async def test_stage_tags_never_reach_the_transcript():
    audio = preroll.PreparedAudio()
    call = SimpleNamespace(id=uuid4(), transcript=[], add_transcript=lambda s, t: call.transcript.append((s, t)))
    await preroll._pump(call, audio, FakeSession([_agent_line('<no speech>{pause}')]))
    assert call.transcript == [] and audio.agent_said == ''


def _frame(level):
    import numpy as np
    return (np.ones(160, dtype='<i2') * level).tobytes()


async def test_caller_talking_over_the_agent_stops_its_audio(monkeypatch):
    import asyncio
    from app.plivo.websocket import play_audio
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_BARGE_IN_SECONDS', 0.05)
    audio = preroll.PreparedAudio()
    audio.attached, audio.stream_id = True, 'stream-1'
    await audio.push(audio.generation, play_audio(b'\0\0' * 160))  # Agent still has audio queued.
    await bridge.on_caller_audio(audio, _frame(3000), session=None)
    assert audio.queue.qsize() == 1  # A single loud frame (cough, click) does not cut the agent off.
    await asyncio.sleep(0.06)
    await bridge.on_caller_audio(audio, _frame(3000), session=None)
    events = [audio.queue.get_nowait()[1]['event'] for _ in range(audio.queue.qsize())]
    assert events == ['clearAudio']


async def test_quiet_line_noise_is_not_speech():
    from app.telephony import bridge
    audio = preroll.PreparedAudio()
    await bridge.on_caller_audio(audio, _frame(50), session=None)
    assert audio.voice_started is None and audio.caller_voice_at == 0.0


async def test_no_nudge_while_the_caller_is_still_talking(monkeypatch):
    import asyncio, time
    from app.telephony import bridge
    monkeypatch.setattr(bridge.settings, 'CALL_REPLY_NUDGE_SECONDS', 0.1)
    monkeypatch.setattr(bridge.settings, 'CALL_SILENCE_SECONDS', 30)
    sent = []
    async def send_instruction(session, text):
        sent.append(text)
    audio = preroll.PreparedAudio()
    audio.manager = SimpleNamespace(send_instruction=send_instruction)
    audio.caller_said, audio.caller_turns, audio.caller_spoke_at = 'ഞാൻ ഇപ്പോൾ', 1, time.monotonic()
    task = asyncio.create_task(bridge.watch_silence(audio, SimpleNamespace(id=uuid4()), tick=0.02))
    for _ in range(10):  # Still talking: loud audio keeps arriving.
        audio.caller_voice_at = time.monotonic()
        await asyncio.sleep(0.03)
    assert sent == []
    await asyncio.sleep(0.25)  # Now they stop.
    task.cancel()
    assert len(sent) == 1
