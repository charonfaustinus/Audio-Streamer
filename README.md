# Audio-Streamer
GUI app for streaming audio to RTMP (audio only) and Icecast with metadata. This was made for [MediaMTX](https://github.com/bluenviron/mediamtx) and [Icecast](https://icecast.org/) combo setup, Assume that you have server set to the same password.  

Might work on linux and mac, I didn't have devices to test it. This was made to be super light and barebone. If you want more advance option and "Video support" please use [OBS](https://github.com/obsproject/obs-studio/)

<img width="549" height="832" alt="Screenshot 2026-03-06 205803" src="https://github.com/user-attachments/assets/8be48793-16c7-47e9-a38c-c63120c59dcb" />


Requirements
```
pyaudiowpatch
numpy
requests
ttkbootstrap
winsdk
```
and [FFmpeg](https://ffmpeg.org/download.html) in your system path or within the same folder of the script.

# How to use
Select your audio device to capture from. The desktop audio device will be have `[loopback]` at the end  
`RTMP Server`: Only put your ip and port in, example `rtmp://localhost.com:1936`. if nothing has been put in, it will disabled the streaming for RTMP automatically.  
`Icecast Server`: Only put your ip and port in, example `localhost.com:8000`. if nothing has been put in, it will disabled the streaming for Icecast automatically.  
`Username`: Your Icecast admin / MediaMTX username  
`Password`: Your Icecast source / MediaMTX password  
`Stream Name:` Your stream name. It'll use as Icecast mouth point name, and RTMP mediamtx Stream name. The final Icecast url will be `icecast://source:password@ip:port/StreamName` and RTMP will be `rtmp://ip:port/StreamName?user=username&pass=password` This was made for setup that have the same username/password on both Icecast and MediaMTX  

# Download
[Here](https://github.com/charonfaustinus/Audio-Streamer/releases/tag/Release)
