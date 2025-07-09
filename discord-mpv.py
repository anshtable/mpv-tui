#!/usr/bin/env python3
import subprocess
import sys
import threading
import time
import os
import json
import socket
from pypresence import Presence

CLIENT_ID = "1391414654001217556"  # mpv with pre-uploaded assets

class MPVDiscordRPC:
    def __init__(self, socket_path):
        self.rpc = None
        self.connected = False
        self.current_title = ""
        self.current_artist = ""
        self.mpv_socket = socket_path
        self.running = True
        
    def connect_discord(self):
        """Connect to Discord RPC"""
        try:
            self.rpc = Presence(CLIENT_ID)
            self.rpc.connect()
            self.connected = True
            print("Discord RPC connected")
        except Exception as e:
            print(f"Failed to connect to Discord: {e}")
            self.connected = False
    
    def get_mpv_property(self, property_name):
        """Get property from mpv via IPC"""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(self.mpv_socket)
            
            command = {
                "command": ["get_property", property_name],
                "request_id": 1
            }
            
            sock.send((json.dumps(command) + '\n').encode())
            response = sock.recv(4096).decode()
            sock.close()
            
            data = json.loads(response)
            if data.get("error") == "success":
                return data.get("data", "")
            return ""
        except Exception as e:
            print(f"Error getting property {property_name}: {e}")
            return ""
    
    def update_metadata(self):
        """Update song metadata from mpv"""
        title = self.get_mpv_property("media-title")
        if not title:
            title = self.get_mpv_property("metadata/by-key/Title")
        if not title:
            title = self.get_mpv_property("metadata/by-key/title")
        
        artist = self.get_mpv_property("metadata/by-key/Artist")
        if not artist:
            artist = self.get_mpv_property("metadata/by-key/artist")
        if not artist:
            artist = self.get_mpv_property("metadata/by-key/ARTIST")
        
        self.current_title = title or "Unknown Title"
        
        if artist and artist.strip():
            self.current_artist = artist.strip()
        elif " - " in self.current_title:
            parts = self.current_title.split(" - ", 1)
            self.current_artist = parts[0].strip()
            self.current_title = parts[1].strip()
        else:
            self.current_artist = ""
    
    def update_presence(self):
        """Update Discord presence"""
        if not self.connected:
            return
            
        try:
            self.update_metadata()
            
            if self.current_artist:
                details_text = f"by {self.current_artist}"
                state_text = self.current_title
            else:
                details_text = "Listening to music"
                state_text = self.current_title
            
            # Truncate if too long
            details_text = details_text[:128]
            state_text = state_text[:128]
            
            self.rpc.update(
                details=details_text,
                state=state_text,
                large_image="mpv",
                large_text="Mpv Media Player",
                small_image="play",
                small_text="Playing"
            )
        except Exception as e:
            print(f"Failed to update Discord presence: {e}")
    
    def monitor_mpv(self):
        """Monitor mpv for metadata changes"""
        while self.running:
            try:
                if not os.path.exists(self.mpv_socket):
                    time.sleep(2)
                    continue
                    
                self.update_presence()
                time.sleep(15)
            except Exception as e:
                print(f"Monitoring error: {e}")
                time.sleep(5)
    
    def stop(self):
        """Clean up and stop the RPC"""
        self.running = False
        if self.connected and self.rpc:
            try:
                self.rpc.close()
            except:
                pass

def main(socket_path):
    rpc = MPVDiscordRPC(socket_path)
    try:
        rpc.connect_discord()
        rpc.monitor_mpv()
    except KeyboardInterrupt:
        pass
    finally:
        rpc.stop()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: discordmpv.py <socket_path>")
        sys.exit(1)
