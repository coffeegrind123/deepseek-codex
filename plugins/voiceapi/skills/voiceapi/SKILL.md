---
name: voiceapi
description: Turn recordings into custom voices and speak with them through the self-hosted voiceapi. Use when the user drops or names an audio/video file and asks to make, clone or create a voice from it; asks to say or read something in a voice; or asks to transcribe or clean up a recording.
---

# Voice API

The `voiceapi` MCP tools work on **local files**: they upload them to the voiceapi gateway themselves.
Never read audio into the conversation, base64 it, or curl the gateway yourself.

Ovis puts a dropped file in the message as an absolute path ending in `/.ovis/<name>`. Pass that
path as is.

## The default voice persists

voiceapi stores voices and a **default voice** itself. They outlive this conversation: a new
session with no memory of earlier chats still has them. So:

- Never assume there is no voice because you do not remember creating one. Check with
  `get_default_voice` or `list_voices`.
- A voice made from a dropped file becomes the default automatically (`make_default` defaults to
  true) and stays the default until the user asks to change it or turn it off.
- To speak, omit `voice` and the default is used. Name a voice only when the user picks one.
- Change it with `set_default_voice(voice=<name>)`, or turn it off with `set_default_voice(voice=null)`,
  only when the user asks.

## Make a voice from a file

1. Name: use the one the user gave. If none, ask, or derive a short one from context and say so.
2. Call `create_voice_from_file` with `paths=[<the dropped path>]` and `name`. Pass `transcript`
   only when the user gave the exact words of a single clip.
3. Report the result in a few lines: the voice name, the reference transcript, the cloning mode,
   the SNR improvement from cleaning, and that it is now the default voice for future sessions
   (pass `make_default=false` only if the user said not to). Offer to say something with it.

Several files of the same speaker make a steadier voice: pass them all in `paths`.

## Speak

You cannot play audio: you run in a container with no speakers, and a headless browser plays
nowhere. Never open a browser or any player to "play" a file, and never claim you played it.
Saving the file is the whole job. The user hears it by opening the path, and in Ovis replies are
narrated automatically. "Say X out loud" means: call `speak` and give the path.

Call `speak` with `text`, leaving out `voice` to use the default (or naming one the user picked).
Save next to the conversation so the user can open it: `out_path=<directory of the dropped file or the project>/.ovis/<short-name>.mp3`. Put the
returned path in your reply.

## Transcribe or clean

`transcribe_file(path)` for the words; `enhance_file(path)` writes `<name>-clean.wav` next to the
input.

## Errors

Tool errors start with a code and often carry a `Hint:`. Follow the hint. Common ones:

- `CONFIG`: `VOICEAPI_KEY` / `VOICEAPI_URL` missing from `.codex/secrets.env`; tell the user.
- `UNREACHABLE`: the voiceapi stack is down, or Codex runs with `CODEX_NET=restricted`.
- `AUDIO_TOO_SHORT`: the clips hold under 3 s of speech after trimming; ask for a longer recording.
- `VOICE_NAME_TAKEN`: pick another name or ask.
- `UNSUPPORTED_FILE`: not an audio or video file.
