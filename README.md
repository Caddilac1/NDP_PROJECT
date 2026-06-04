# RTSP Video Streaming Client and Server

## Overview

This project is a group submission for Networking and  Distributed Systems Programming course. The objective of the project is to design and implement a video streaming system using the Real-Time Streaming Protocol (RTSP).

The system consists of two main components:

- **RTSP Server** – Responsible for managing client requests and streaming video data.
- **RTSP Client** – A graphical application built with Python and Tkinter that allows users to connect to the server and control video playback.

---

## Features

### Client Features

- Graphical User Interface (GUI) built with Tkinter
- Stream URL input
- RTSP session management
- Video display area
- Playback controls:
  - SETUP
  - PLAY
  - PAUSE
  - TEARDOWN

### Server Features

- Accepts RTSP client connections
- Processes RTSP requests
- Streams video frames to connected clients
- Manages streaming sessions

---

## Technologies Used

- Python 3
- Tkinter
- Socket Programming
- RTSP Protocol
- RTP Streaming

---

## Project Structure

```text
project/
│
├── client.py
├── server.py
├── README.md
├── requirements.txt
└── assets/
```

---

## RTSP Workflow

```text
SETUP → PLAY → PAUSE → TEARDOWN
```

### SETUP

Establishes communication with the server and prepares the streaming session.

### PLAY

Starts receiving and displaying video frames.

### PAUSE

Temporarily stops video playback.

### TEARDOWN

Terminates the session and releases resources.

---

## Installation

### Clone the Repository

```bash
git clone https://github.com/Caddilac1/NDP_PROJECT.git
cd NDP_PROJECT
```

### Create a Virtual Environment

#### Linux/macOS

```bash
python3 -m venv venv
source venv/bin/activate
```

#### Windows

```bash
python -m venv venv
venv\Scripts\activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Running the Application

### Start the Server

Open a terminal and run:

```bash
python server.py
```

The server should start listening for incoming RTSP connections.

### Start the Client

Open another terminal and run:

```bash
python client.py
```

The client GUI will launch.

---

## Using the Client

1. Enter the RTSP stream URL.
2. Click **SETUP** to initialize the streaming session.
3. Click **PLAY** to begin video playback.
4. Click **PAUSE** to pause the stream.
5. Click **TEARDOWN** to terminate the session.

---
