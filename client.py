import tkinter as tk

def connect():
    pass

def disconnect():
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

connect_button = tk.Button(top_frame, text="Connect", command=connect)
connect_button.pack(side="left", padx=5)

disconnect_button = tk.Button(top_frame, text="Disconnect", command=disconnect)
disconnect_button.pack(side="left", padx=5)

'''Main frame for video display'''
'''Control buttons frame for play, pause, stop, teardown'''
root.mainloop()