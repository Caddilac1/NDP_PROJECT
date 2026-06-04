import tkinter as tk

def connect():
    connection_status.config(text="Status: Connected", fg="green")

# def disconnect():
#     pass

def setup():
    pass

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