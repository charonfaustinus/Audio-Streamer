import pyaudiowpatch as pyaudio
import numpy as np
import subprocess as sp
import threading
import time
import requests
import tkinter.messagebox as messagebox
import ttkbootstrap as ttk
import asyncio
import winrt.windows.media.control as wmc
import configparser
import os
import sys

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ConfigParser
config = configparser.ConfigParser()
config.read('config.ini')
RTMP_BASE_URL = config.get('Connection', 'rtmp_url', fallback='')
ICECAST_BASE_URL = config.get('Connection', 'icecast_url', fallback='')
USERNAME = config.get('Connection', 'username', fallback='')
PASSWORD = config.get('Connection', 'password', fallback='')
STREAM_NAME = config.get('Connection', 'stream_name', fallback='stream')

# global var
p = pyaudio.PyAudio()
DTYPE = np.float32 
AUDIO_FORMAT = 'f32le'

audio_level = 0.0
level_lock = threading.Lock()

running = False
stream = None
metadata_thread = None
rtmp_proc = None
icecast_proc = None
rtmp_monitor = None
icecast_monitor = None
samplerate = None
channels = None
startupinfo = None
RTMP_URL = None
ICECAST_URL = None
ICECAST_ADMIN_URL = None
ICECAST_MOUNT = None

# get current track using Microslop Windows Media API
async def get_media_info():
    try:
        sessions = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        current_session = sessions.get_current_session()
        if current_session:
            playback_info = current_session.get_playback_info()
            if playback_info and playback_info.playback_status == wmc.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING: #fucking long ass function name
                media_props = await current_session.try_get_media_properties_async()
                if media_props:
                    artist = media_props.artist
                    title = media_props.title
                    if artist and title:
                        return f"{artist} - {title}"
        return None
    except Exception as e:
        print(f"Error getting media info: {e}")
        return None

def get_current_track():
    return asyncio.run(get_media_info())

# update icecast metadata
def update_icecast_metadata(metadata):
    if metadata:
        params = {
            'mount': ICECAST_MOUNT,
            'mode': 'updinfo',
            'song': metadata
        }
        try:
            response = requests.get(ICECAST_ADMIN_URL, params=params, auth=(USERNAME, PASSWORD))
            print(f"Metadata update status: {response.status_code}")
        except Exception as e:
            print(f"Error updating metadata: {e}")

# metadata monitoring thread
def metadata_monitor():
    current_metadata = None
    while running:
        new_metadata = get_current_track()
        if new_metadata != current_metadata:
            current_metadata = new_metadata
            update_icecast_metadata(new_metadata)
            now_playing_var.set(new_metadata if new_metadata else "No Media Playing")
        time.sleep(5)

# audio processing
def audio_callback(audio_data, frame_count, time_info, status):
    if not running:
        return (None, pyaudio.paAbort)

    try:
        if rtmp_proc is not None:
            rtmp_proc.stdin.write(audio_data)
        if icecast_proc is not None:
            icecast_proc.stdin.write(audio_data)
    except (BrokenPipeError, AttributeError, OSError, ValueError):
        pass  

    # VU meter
    data = np.frombuffer(audio_data, dtype=np.float32)
    peak = np.max(np.abs(data))
    db = 20 * np.log10(peak + 1e-6)
    level = max(0, min(100, (db + 35) / 35 * 100))
    with level_lock:
        global audio_level
        audio_level = level
    
    return (None, pyaudio.paContinue)

# Monitor and restart ffmpeg process
def monitor_and_restart(create_proc_func, status_var, label):
    global running
    while running:
        proc = create_proc_func()
        status_var.set("Connecting")

        while running:
            if proc.poll() is not None:
                if proc.returncode != 0:
                    status_var.set(f"⚠ Error ({proc.returncode})")
                    break
                else:
                    return

            try:
                line = proc.stderr.readline()
                if not line:
                    time.sleep(0.1)
                    continue

                line = line.decode("utf-8", errors="ignore").strip()
                print(f"[{label}] {line}")

                lower = line.lower()

                if "error" in lower or "failed" in lower:
                    status_var.set("❌ Error")
                    break

                if "encoder" in lower or "time=" in lower:
                    status_var.set("▶ Streaming")

            except Exception:
                pass

            time.sleep(0.1)

        # Clean up the process
        try:
            proc.stdin.close()
            status_var.set("🟥 Stopped")
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except sp.TimeoutExpired:
                proc.kill()
                proc.wait()

        if running:
            status_var.set("❌ Disconnected")
            time.sleep(3)

def create_rtmp_proc():
    global rtmp_proc
    rtmp_codec = rtmp_codec_var.get()
    rtmp_bitrate = rtmp_bitrate_var.get()
    if rtmp_codec == 'aac':
        rtmp_audio_opts = ['-c:a', 'aac', '-b:a', rtmp_bitrate]
    elif rtmp_codec == 'mp3':
        rtmp_audio_opts = ['-c:a', 'libmp3lame', '-b:a', rtmp_bitrate]
    base = [
        'ffmpeg',
        '-hide_banner',
        '-y',
        '-f', AUDIO_FORMAT,
        '-ar', str(samplerate),
        '-ac', str(channels),
        '-i', 'pipe:0'
    ]
    rtmp_command = base + rtmp_audio_opts + ['-f', 'flv', RTMP_URL]
    rtmp_proc = sp.Popen(
        rtmp_command,
        stdin=sp.PIPE,
        stderr=sp.PIPE,
        startupinfo=startupinfo
    )
    return rtmp_proc

def create_icecast_proc():
    global icecast_proc
    ice_codec = ice_codec_var.get()
    ice_bitrate = ice_bitrate_var.get()
    if ice_codec == 'mp3':
        ice_audio_opts = ['-c:a', 'libmp3lame', '-b:a', ice_bitrate, '-content_type', 'audio/mpeg', '-f', 'mp3', '-method', 'PUT', '-auth_type', 'basic', '-chunked_post', '1', '-send_expect_100', '0']
    elif ice_codec == 'aac':
        ice_audio_opts = ['-c:a', 'aac', '-b:a', ice_bitrate, '-content_type', 'audio/aac', '-f', 'adts', '-method', 'PUT', '-auth_type', 'basic', '-chunked_post', '1', '-send_expect_100', '0']
    elif ice_codec == 'opus':
        ice_audio_opts = ['-c:a', 'libopus', '-b:a', ice_bitrate, '-content_type', 'audio/ogg', '-f', 'ogg', '-method', 'PUT', '-auth_type', 'basic', '-chunked_post', '1', '-send_expect_100', '0']
    elif ice_codec == 'ogg':
        ice_audio_opts = ['-c:a', 'libvorbis', '-b:a', ice_bitrate, '-content_type', 'audio/ogg', '-f', 'ogg', '-method', 'PUT', '-auth_type', 'basic', '-chunked_post', '1', '-send_expect_100', '0']
    elif ice_codec == 'flac':
        ice_audio_opts = ['-c:a', 'flac', '-content_type', 'audio/ogg', '-f', 'ogg', '-method', 'PUT', '-auth_type', 'basic', '-chunked_post', '1', '-send_expect_100', '0']
    ice_meta_opts = [
        '-ice_name', ice_name_var.get(),
        '-ice_description', ice_desc_var.get(),
        '-ice_genre', ice_genre_var.get()
    ]
    base = [
        'ffmpeg',
        '-hide_banner',
        '-y',
        '-f', AUDIO_FORMAT,
        '-ar', str(samplerate),
        '-ac', str(channels),
        '-i', 'pipe:0'
    ]
    icecast_command = base + ice_audio_opts + ice_meta_opts + [ICECAST_URL]
    icecast_proc = sp.Popen(
        icecast_command,
        stdin=sp.PIPE,
        stderr=sp.PIPE,
        startupinfo=startupinfo
    )
    return icecast_proc

def start_streaming(device_index):
    global running
    global RTMP_URL, ICECAST_URL
    global ICECAST_ADMIN_URL, ICECAST_MOUNT
    global USERNAME, PASSWORD, STREAM_NAME
    global rtmp_proc, icecast_proc
    global rtmp_monitor, icecast_monitor
    global stream, metadata_thread
    global samplerate, channels
    global startupinfo

    rtmp_base = rtmp_url_var.get()
    ice_base = ice_url_var.get().replace("http://", "").replace("https://", "").rstrip('/')

    USERNAME = username_var.get()
    PASSWORD = password_var.get()
    STREAM_NAME = stream_name_var.get().lstrip('/')

    RTMP_URL = f"{rtmp_base}/{STREAM_NAME}?user={USERNAME}&pass={PASSWORD}" if rtmp_base else None
    ICECAST_URL = f"icecast://source:{PASSWORD}@{ice_base}/{STREAM_NAME}" if ice_base else None

    ICECAST_ADMIN_URL = f"http://{ice_base}/admin/metadata" if ice_base else None
    ICECAST_MOUNT = f"/{STREAM_NAME}" if ice_base else None

    startupinfo = None
    if os.name == 'nt':
        startupinfo = sp.STARTUPINFO()
        startupinfo.dwFlags |= sp.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    device_info = devices[device_index]
    samplerate = int(device_info["defaultSampleRate"])

    selected_name = device_var.get()
    max_ch = device_info["maxInputChannels"]

    if max_ch <= 0:
        print("Audio device has invalid channel count.")
        return

    preferred_channels = 2 if max_ch >= 2 else 1
    channels = preferred_channels
    try:
        p.is_format_supported(samplerate, input_device=device_index, input_channels=channels, input_format=pyaudio.paFloat32)
    except ValueError as e:
        if "invalid number of channels" in str(e).lower():
            alternative_channels = 1 if channels == 2 else 2
            try:
                p.is_format_supported(samplerate, input_device=device_index, input_channels=alternative_channels, input_format=pyaudio.paFloat32)
                channels = alternative_channels
                print(f"Fallback to {channels} channels.")
            except ValueError as e2:
                print(f"Neither {preferred_channels} nor {alternative_channels} channels supported: {e2}")
                return

    print(f"Using {channels} channels for device: {selected_name}")

    enable_rtmp = bool(rtmp_base)
    enable_icecast = bool(ice_base)

    if not enable_rtmp and not enable_icecast:
        print("No streams enabled.")
        return

    stream = p.open(
        format=pyaudio.paFloat32,
        channels=channels,
        rate=samplerate,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=2048,
        stream_callback=audio_callback
    )
    stream.start_stream()

    running = True

    if enable_rtmp:
        rtmp_monitor = threading.Thread(target=monitor_and_restart, args=(create_rtmp_proc, rtmp_status_var, 'RTMP'), daemon=True)
        rtmp_monitor.start()
    else:
        rtmp_status_var.set("Disabled")

    if enable_icecast:
        icecast_monitor = threading.Thread(target=monitor_and_restart, args=(create_icecast_proc, icecast_status_var, 'Icecast'), daemon=True)
        icecast_monitor.start()
        metadata_thread = threading.Thread(target=metadata_monitor, daemon=True)
        metadata_thread.start()
    else:
        icecast_status_var.set("Disabled")

    save_config()

def save_config():
    config = configparser.ConfigParser()

    config['Connection'] = {
        'rtmp_url': rtmp_url_var.get(),
        'icecast_url': ice_url_var.get(),
        'username': username_var.get(),
        'password': password_var.get(),
        'stream_name': stream_name_var.get()
    }

    config['RTMP'] = {
        'codec': rtmp_codec_var.get(),
        'bitrate': rtmp_bitrate_var.get()
    }

    config['Icecast'] = {
        'codec': ice_codec_var.get(),
        'bitrate': ice_bitrate_var.get(),
        'name': ice_name_var.get(),
        'description': ice_desc_var.get(),
        'genre': ice_genre_var.get()
    }

    with open('config.ini', 'w') as configfile:
        config.write(configfile)

def stop_streaming():
    global running, stream, rtmp_proc, icecast_proc, rtmp_monitor, icecast_monitor, metadata_thread
    if not running:
        return

    running = False
    level_var = 0

    if stream:
        stream.stop_stream()
        stream.close()

    if rtmp_proc and rtmp_proc.poll() is None:
        try:
            rtmp_proc.stdin.close()
        except Exception:
            pass
        if rtmp_proc.poll() is None:
            rtmp_proc.terminate()
            try:
                rtmp_proc.wait(timeout=3)
            except sp.TimeoutExpired:
                rtmp_proc.kill()
                rtmp_proc.wait()
    if icecast_proc and icecast_proc.poll() is None:
        try:
            icecast_proc.stdin.close()
        except Exception:
            pass
        if icecast_proc.poll() is None:
            icecast_proc.terminate()
            try:
                icecast_proc.wait(timeout=3)
            except sp.TimeoutExpired:
                icecast_proc.kill()
                icecast_proc.wait()

    rtmp_status_var.set("🟥 Stopped")
    icecast_status_var.set("🟥 Stopped")

# GUI --- ttk is just make things looks like windows 98 with a dark theme
root = ttk.Window(themename="cyborg")
root.resizable(width=False, height=False)

style = ttk.Style()
style.configure(".", font=("Helvetica", 12))
root.columnconfigure(0, weight=1)

#icon
icon_path = resource_path("icon.ico")
try:
    root.iconbitmap(icon_path)
except Exception:
    pass
root.title("Audio Streamer v1.0")


hostapis = list(p.get_host_api_info_generator())
wasapi_index = next((h['index'] for h in hostapis if 'WASAPI' in h['name']), None)
devices = list(p.get_device_info_generator())

loopback_indices = [d['index'] for d in list(p.get_loopback_device_info_generator())]
mic_devices = [
    (d['index'], d['name'])
    for d in devices
    if d['maxInputChannels'] > 0 and d['hostApi'] == wasapi_index and d['index'] not in loopback_indices
]
loopback_devices = [
    (d['index'], d['name'])
    for d in list(p.get_loopback_device_info_generator())
]
input_devices = mic_devices + loopback_devices
input_devices.sort(key=lambda x: x[1])

# Device selection
ttk.Label(root, text="Select Audio Capture Device:").pack(pady=3)
device_var = ttk.StringVar()
device_combobox = ttk.Combobox(root, textvariable=device_var, 
                               values=[name for _, name in input_devices], width=60, state='readonly')
device_combobox.pack(pady=5, padx=10)

# Connection Settings
conn_frame = ttk.LabelFrame(root, text="Connection Settings")
conn_frame.pack(pady=5, padx=10, fill='x')
conn_frame.columnconfigure(1, weight=1)
conn_frame.columnconfigure(3, weight=1)

rtmp_url_var = ttk.StringVar(value=RTMP_BASE_URL)
ice_url_var = ttk.StringVar(value=ICECAST_BASE_URL)
username_var = ttk.StringVar(value=USERNAME)
password_var = ttk.StringVar(value=PASSWORD)
stream_name_var = ttk.StringVar(value=STREAM_NAME)

ttk.Label(conn_frame, text="RTMP Server:").grid(row=0, column=0, sticky='e', padx=5, pady=3)
rtmp_entry = ttk.Entry(conn_frame, textvariable=rtmp_url_var)
rtmp_entry.grid(row=0, column=1, sticky='ew', padx=5, pady=3, columnspan=3)

ttk.Label(conn_frame, text="Icecast Server:").grid(row=1, column=0, sticky='e', padx=5, pady=3)
ice_entry = ttk.Entry(conn_frame, textvariable=ice_url_var)
ice_entry.grid(row=1, column=1, sticky='ew', padx=5, pady=3, columnspan=3)

ttk.Label(conn_frame, text="Username:").grid(row=2, column=0, sticky='e', padx=5, pady=3)
username_entry = ttk.Entry(conn_frame, textvariable=username_var)
username_entry.grid(row=2, column=1, sticky='ew', padx=5, pady=3)

ttk.Label(conn_frame, text="Password:").grid(row=2, column=2, sticky='e', padx=5, pady=3)
password_entry = ttk.Entry(conn_frame, textvariable=password_var, show="*")
password_entry.grid(row=2, column=3, sticky='ew', padx=5, pady=3)

ttk.Label(conn_frame, text="Stream Name:").grid(row=5, column=0, sticky='e', padx=5, pady=3)
stream_name_entry = ttk.Entry(conn_frame, textvariable=stream_name_var)
stream_name_entry.grid(row=5, column=1, sticky='ew', padx=5, pady=3, columnspan=3)

show_pw = ttk.BooleanVar(value=False)
def toggle_pw():
    password_entry.config(show="" if show_pw.get() else "*")

ttk.Checkbutton(conn_frame, text="Show Password", variable=show_pw, command=toggle_pw)\
    .grid(row=4, column=3, sticky='e', padx=5, pady=3)

# RTMP Settings
rtmp_frame = ttk.LabelFrame(root, text="RTMP Settings")
rtmp_frame.pack(pady=5, padx=10, fill='x')
rtmp_frame.columnconfigure(1, weight=1)
rtmp_frame.columnconfigure(3, weight=1)

ttk.Label(rtmp_frame, text="Codec:").grid(row=0, column=0, padx=5, pady=3, sticky='e')
rtmp_codec_var = ttk.StringVar(value=config.get('RTMP', 'codec', fallback='aac'))
rtmp_codec_combobox = ttk.Combobox(rtmp_frame, textvariable=rtmp_codec_var, values=['aac', 'mp3'], state='readonly')
rtmp_codec_combobox.grid(row=0, column=1, padx=5, pady=10, sticky='ew')

ttk.Label(rtmp_frame, text="Bitrate:").grid(row=0, column=2, padx=5, pady=3, sticky='e')
rtmp_bitrate_var = ttk.StringVar(value=config.get('RTMP', 'bitrate', fallback='128k'))
rtmp_bitrate_combobox = ttk.Combobox(rtmp_frame, textvariable=rtmp_bitrate_var, 
                                     values=['8k', '16k', '32k', '64k', '96k', '128k', '192k', '256k', '320k'], state='readonly')
rtmp_bitrate_combobox.grid(row=0, column=3, padx=5, pady=10, sticky='ew')

# Icecast Settings
ice_frame = ttk.LabelFrame(root, text="Icecast Settings")
ice_frame.pack(pady=5, padx=10, fill='x')
ice_frame.columnconfigure(1, weight=1)
ice_frame.columnconfigure(3, weight=1)

ttk.Label(ice_frame, text="Stream Name:").grid(row=0, column=0, padx=5, pady=1, sticky='e')
ice_name_var = ttk.StringVar(value=config.get('Icecast', 'name', fallback='My Stream'))
ttk.Entry(ice_frame, textvariable=ice_name_var, width=30).grid(row=0, column=1, padx=5, pady=3, columnspan=3, sticky='ew')

ttk.Label(ice_frame, text="Stream Description:").grid(row=1, column=0, padx=5, pady=1, sticky='e')
ice_desc_var = ttk.StringVar(value=config.get('Icecast', 'description', fallback='My Stream Description'))
ttk.Entry(ice_frame, textvariable=ice_desc_var, width=30).grid(row=1, column=1, padx=5, pady=3, columnspan=3, sticky='ew')

ttk.Label(ice_frame, text="Genre:").grid(row=2, column=0, padx=5, pady=1, sticky='e')
ice_genre_var = ttk.StringVar(value=config.get('Icecast', 'genre', fallback='Various'))
ttk.Entry(ice_frame, textvariable=ice_genre_var, width=30).grid(row=2, column=1, padx=5, pady=3, columnspan=3, sticky='ew')

ttk.Label(ice_frame, text="Codec:").grid(row=3, column=0, padx=5, pady=10, sticky='e')
ice_codec_var = ttk.StringVar(value=config.get('Icecast', 'codec', fallback='mp3'))
ice_codec_combobox = ttk.Combobox(ice_frame, textvariable=ice_codec_var, 
                                  values=['mp3', 'aac', 'opus', 'ogg', 'flac'], state='readonly')
ice_codec_combobox.grid(row=3, column=1, padx=5, pady=10, sticky='ew')

ttk.Label(ice_frame, text="Bitrate:").grid(row=3, column=2, padx=5, pady=10, sticky='w')
ice_bitrate_var = ttk.StringVar(value=config.get('Icecast', 'bitrate', fallback='128k'))
ice_bitrate_combobox = ttk.Combobox(ice_frame, textvariable=ice_bitrate_var, 
                                    values=['8k', '16k', '32k', '64k', '96k', '128k', '192k', '256k', '320k'], state='readonly')
ice_bitrate_combobox.grid(row=3, column=3, padx=5, pady=10, sticky='ew')

# Status
status_frame = ttk.LabelFrame(root, text="Status")
status_frame.pack(pady=5, padx=10, fill='x')
status_frame.columnconfigure(1, weight=1)
status_frame.columnconfigure(3, weight=1)

ttk.Label(status_frame, text="RTMP Status: ").grid(row=0, column=0, padx=5, pady=10, sticky='e')
rtmp_status_var = ttk.StringVar(value="Not Started")
ttk.Label(status_frame, textvariable=rtmp_status_var).grid(row=0, column=1, padx=5, pady=10, sticky='w')

ttk.Label(status_frame, text="Icecast Status: ").grid(row=0, column=2, sticky='e')
icecast_status_var = ttk.StringVar(value="Not Started")
ttk.Label(status_frame, textvariable=icecast_status_var).grid(row=0, column=3, padx=5, pady=10, sticky='w')

# VU meter
uv_frame = ttk.LabelFrame(root, text="Audio Level")
uv_frame.pack(pady=5, padx=10, fill='x')
uv_frame.columnconfigure(0, weight=1)
level_var = ttk.DoubleVar()
vu_progress = ttk.Progressbar(uv_frame, orient="horizontal", mode="determinate", 
                              maximum=100, variable=level_var, bootstyle="success")
vu_progress.grid(row=0, column=0, padx=5, pady=10, sticky='ew')

# Now playing
nowplaying_frame = ttk.LabelFrame(root, height=80, text="Now Playing")
nowplaying_frame.pack(pady=5, padx=10, fill='x')
nowplaying_frame.grid_propagate(False) 
nowplaying_frame.columnconfigure(0, weight=1)
nowplaying_frame.rowconfigure(0, weight=1)
now_playing_var = ttk.StringVar(value="Waiting for media...")
ttk.Label(nowplaying_frame, textvariable=now_playing_var, wraplength=450, justify='center', anchor='center', bootstyle="success").grid(row=0, column=0, padx=5, pady=10, sticky='nsew')


def update_vu():
    with level_lock:
        level_var.set(audio_level)
    root.after(50, update_vu)

def update_start_button():
    if running:
        start_button.config(text="Streaming", state="disabled")
    else:
        start_button.config(text="Start Streaming", state="normal")

# Start Streaming button
def on_start():
    if running:
        messagebox.showinfo("Alert", "Already Streaming.")
        return
    selected_name = device_var.get()
    if not selected_name:
        messagebox.showinfo("Alert", "No device selected.")
        return
    device_index = next((i for i, name in input_devices if name == selected_name), None)
    if device_index is not None:
        start_streaming(device_index)
        update_start_button()
    else:
        messagebox.showinfo("Alert", "Device not found.")

# Stop Streaming button
def on_stop():
    stop_streaming()
    update_start_button()
        
# Buttons
button_frame = ttk.LabelFrame(root)
button_frame.pack(pady=5, padx=10, fill='x')
button_frame.columnconfigure(0, weight=1)
button_frame.columnconfigure(1, weight=1)

start_button = ttk.Button(button_frame, text="Start Streaming", command=on_start, bootstyle="success")
start_button.grid(row=0, column=0, padx=5, pady=10, sticky='ew')
ttk.Button(button_frame, text="Stop Streaming", command=on_stop, bootstyle="danger")\
    .grid(row=0, column=1, padx=5, pady=10, sticky='ew')

# Start VU update
update_vu()
root.mainloop()