import tkinter as tk
import socket



RTSP_PORT = 8554
# cseq = 1
# session_ID = None

def setup():
    url = entry.get()
    url = url.replace("rtsp://", "")
    parts = url.split("/")
    server_ip = parts[0]
    
    if not url:
        connection_status.config(text="Status: Please enter a stream URL", fg="red")
        return
    
    rtsp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM) 
    
    rtsp_socket.connect((server_ip, RTSP_PORT))
    
    connection_status.config(text="Status: Streaming", fg="green")
    
    
    request = (
        f"SETUP rtsp://{url} RTSP/1.0\r\n"
        f"CSeq: 1\r\n"
        f"Transport: RTP/UDP; client_port=5004-5005\r\n\r\n"
    )
    
    rtsp_socket.send(request.encode(("utf-8")))
    
    response = rtsp_socket.recv(4096).decode()
    
    print(response)
    
    import re
    match = re.search(r"Session:\s*(\d+)", response)

    if match:
        session_id = match.group(1)
        print("Stored session:", session_id)

def play():
    pass

def pause():
    pass

def teardown():
    pass

'''Main application windoww setup'''
root = tk.Tk()
root.title("Group 5")
root.geometry("1280x720")

'''Top frame for stream URL input and connect/disconnect buttons'''
top_frame = tk.Frame(root)
top_frame.pack(pady=10)

label = tk.Label(top_frame, text="Stream URL:")
label.pack(side="left", padx=5)

entry = tk.Entry(top_frame, width=50)
entry.pack(side="left", padx=5)

connection_status = tk.Label(top_frame, text="Status: Disconnected", fg="red", font=("Arial", 11))
connection_status.pack(side="left", padx=10)

# disconnect_button = tk.Button(top_frame, text="Disconnect", command=disconnect)
# disconnect_button.pack(side="left", padx=5)

'''Main frame for video display'''
video_frame = tk.Frame(root, width=1000,height=500, bg="black", relief="solid", bd=2)
video_frame.pack(pady=20)
video_frame.pack_propagate(False)
video_label = tk.Label(video_frame, text="Video Stream...", bg="black", fg="white", font=("Arial",15))
video_label.pack(expand=True)

'''Control buttons frame for play, pause, stop, teardown'''
bottom_frame = tk.Frame(root)
bottom_frame.pack(pady=10)

setup_button = tk.Button(bottom_frame, text="Setup", command=setup)
setup_button.pack(side="left", padx=5)

play_button = tk.Button(bottom_frame, text="Play", command=play)
play_button.pack(side="left", padx=5)

pause_button = tk.Button(bottom_frame, text="Pause", command=pause)
pause_button.pack(side="left", padx=5)

teardown_button = tk.Button(bottom_frame, text="Teardown", command=teardown)
teardown_button.pack(side="left", padx=5)


root.mainloop()