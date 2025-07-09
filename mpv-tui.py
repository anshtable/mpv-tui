#!/usr/bin/env python3

import curses
import subprocess
import threading
import time
import json
import os
import sys
import signal
import socket
import tempfile
from typing import List, Dict, Optional
import urllib.parse
import re
from pathlib import Path

class HistoryManager:
    def __init__(self):
        # Create config directory
        self.config_dir = Path.home() / '.spotitui'
        self.config_dir.mkdir(exist_ok=True)
        
        # History file path
        self.history_file = self.config_dir / 'history.json'
        self.liked_file = self.config_dir / 'liked.json'
        
        # Load existing data
        self.history = self.load_file(self.history_file)
        self.liked = self.load_file(self.liked_file)
    
    def load_file(self, file_path: Path) -> List[Dict]:
        """Load JSON data from file"""
        try:
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            return []
        except (json.JSONDecodeError, FileNotFoundError, PermissionError):
            return []
    
    def save_file(self, file_path: Path, data: List[Dict]):
        """Save data to JSON file"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except (PermissionError, OSError):
            pass
    
    def add_track(self, track: Dict, to_history: bool = True, to_liked: bool = False):
        """Add track to history and/or liked songs"""
        track_with_time = track.copy()
        track_with_time['played_at'] = time.time()
        
        if to_history:
            # Remove if already in history to avoid duplicates
            self.history = [t for t in self.history if t.get('title') != track['title']]
            self.history.append(track_with_time)
            if len(self.history) > 100:
                self.history = self.history[-100:]
            self.save_file(self.history_file, self.history)
        
        if to_liked:
            # Remove if already in liked to avoid duplicates
            self.liked = [t for t in self.liked if t.get('title') != track['title']]
            self.liked.append(track_with_time)
            self.save_file(self.liked_file, self.liked)
    
    def remove_liked(self, track: Dict):
        """Remove track from liked songs"""
        self.liked = [t for t in self.liked if t.get('title') != track.get('title')]
        self.save_file(self.liked_file, self.liked)
    
    def clear_history(self):
        """Clear all history"""
        self.history.clear()
        self.save_file(self.history_file, self.history)
    
    def clear_liked(self):
        """Clear all liked songs"""
        self.liked.clear()
        self.save_file(self.liked_file, self.liked)
    
    def get_history(self) -> List[Dict]:
        """Get history in reverse order (most recent first)"""
        return list(reversed(self.history))
    
    def get_liked(self) -> List[Dict]:
        """Get liked songs in reverse order (most recent first)"""
        return list(reversed(self.liked))
    
    def is_liked(self, track: Dict) -> bool:
        """Check if a track is liked"""
        return any(t.get('title') == track.get('title') for t in self.liked)

class YouTubeSearcher:
    @staticmethod
    def search(query: str, max_results: int = 10) -> List[Dict]:
        """Search YouTube using yt-dlp"""
        try:
            cmd = [
                'yt-dlp',
                '--dump-json',
                '--flat-playlist',
                '--no-playlist',
                f'ytsearch{max_results}:{query}'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                return []
            
            videos = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        data = json.loads(line)
                        videos.append({
                            'title': data.get('title', 'Unknown'),
                            'url': data.get('url', ''),
                            'duration': data.get('duration', 0),
                            'uploader': data.get('uploader', 'Unknown')
                        })
                    except json.JSONDecodeError:
                        continue
            
            return videos
        except Exception:
            return []

class MPVPlayer:
    def __init__(self):
        self.process = None
        self.current_track = None
        self.is_playing = False
        self.is_paused = False
        self.ipc_socket = None
        self.rpc_process = None
        
    def play(self, url: str, title: str = ""):
        """Play a YouTube URL using mpv"""
        self.stop()
        
        try:
            # Create a unique socket path for IPC
            self.ipc_socket = os.path.join(tempfile.gettempdir(), f"mpv_socket_{os.getpid()}")
            
            # Start Discord RPC handler
            script_dir = os.path.dirname(os.path.abspath(__file__))
            discord_rpc_script = os.path.join(script_dir, 'path-to-discordmpv.py')
            self.rpc_process = subprocess.Popen(
                ['python3', discord_rpc_script, self.ipc_socket],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Start mpv
            self.process = subprocess.Popen([
                'mpv',
                '--no-video',
                '--really-quiet',
                '--no-terminal',
                '--loop',
                f'--input-ipc-server={self.ipc_socket}',
                url
            ])
            self.current_track = title
            self.is_playing = True
            self.is_paused = False
            
            threading.Thread(target=self._monitor_playback, daemon=True).start()
            
        except Exception as e:
            self.current_track = f"Error: {str(e)}"
            self.is_playing = False
    
    def _monitor_playback(self):
        """Monitor mpv process"""
        if self.process:
            self.process.wait()
            self.is_playing = False
            self.is_paused = False
            self.current_track = None
            if self.ipc_socket and os.path.exists(self.ipc_socket):
                try:
                    os.remove(self.ipc_socket)
                except:
                    pass
    
    def _send_command(self, command):
        """Send command to mpv via IPC"""
        if not self.ipc_socket or not os.path.exists(self.ipc_socket):
            return False
        
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.ipc_socket)
            
            command_json = json.dumps({"command": command}) + "\n"
            sock.send(command_json.encode())
            sock.close()
            return True
        except:
            return False
    
    def stop(self):
        """Stop current playback"""
        if self.process:
            self.process.terminate()
            self.process.wait()
            self.process = None
        
        if self.rpc_process:
            self.rpc_process.terminate()
            self.rpc_process.wait()
            self.rpc_process = None
            
        self.is_playing = False
        self.is_paused = False
        self.current_track = None
        
        if self.ipc_socket and os.path.exists(self.ipc_socket):
            try:
                os.remove(self.ipc_socket)
            except:
                pass
        self.ipc_socket = None
    
    def pause(self):
        """Pause/resume playback"""
        if self.process and self.is_playing:
            if self._send_command(["cycle", "pause"]):
                self.is_paused = not self.is_paused
            else:
                try:
                    if self.is_paused:
                        self.process.send_signal(subprocess.signal.SIGCONT)
                        self.is_paused = False
                    else:
                        self.process.send_signal(subprocess.signal.SIGSTOP)
                        self.is_paused = True
                except:
                    pass

class SpotiTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.player = MPVPlayer()
        self.searcher = YouTubeSearcher()
        self.history_manager = HistoryManager()
        
        # UI State
        self.search_results = []
        self.selected_index = 0
        self.search_query = ""
        self.input_mode = False
        self.current_view = "search"  # search, history, liked
        
        # Colors
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)  # Spotify green
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)  # Normal text
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_GREEN)  # Selected
        curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # Playing
        curses.init_pair(5, curses.COLOR_RED, curses.COLOR_BLACK)    # Error
        curses.init_pair(6, curses.COLOR_CYAN, curses.COLOR_BLACK)   # History
        curses.init_pair(7, curses.COLOR_MAGENTA, curses.COLOR_BLACK) # Liked
        
        # Enable mouse support
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.curs_set(0)
        
        # Setup windows
        self.setup_windows()
        
    def setup_windows(self):
        """Setup curses windows"""
        h, w = self.stdscr.getmaxyx()
        
        # Header (logo area)
        self.header_win = curses.newwin(3, w, 0, 0)
        
        # Navigation tabs
        self.nav_win = curses.newwin(2, w, 3, 0)
        
        # Search bar
        self.search_win = curses.newwin(3, w, 5, 0)
        
        # Main content area
        self.main_win = curses.newwin(h - 11, w, 8, 0)
        
        # Now playing bar
        self.player_win = curses.newwin(3, w, h - 3, 0)
        
    def draw_header(self):
        """Draw the header with logo"""
        self.header_win.clear()
        self.header_win.attron(curses.color_pair(1) | curses.A_BOLD)
        
        logo = "♪ SpotiTUI ♪"
        subtitle = "YouTube Music Player"
        
        h, w = self.header_win.getmaxyx()
        self.header_win.addstr(0, (w - len(logo)) // 2, logo)
        self.header_win.attron(curses.color_pair(2))
        self.header_win.addstr(1, (w - len(subtitle)) // 2, subtitle)
        
        self.header_win.refresh()
    
    def draw_navigation(self):
        """Draw navigation tabs"""
        self.nav_win.clear()
        
        h, w = self.nav_win.getmaxyx()
        
        # Tab buttons
        tabs = [
            (" Search ", "search", 1),
            (" History ", "history", 2),
            (" Liked Songs ", "liked", 3)
        ]
        
        x_pos = 2
        for label, view, color in tabs:
            if self.current_view == view:
                self.nav_win.addstr(0, x_pos, label, curses.color_pair(3) | curses.A_BOLD)
            else:
                self.nav_win.addstr(0, x_pos, label, curses.color_pair(color))
            x_pos += len(label) + 2
        
        # Instructions
        instructions = "Click tabs or use 1/2/3 keys to switch views"
        self.nav_win.addstr(1, 2, instructions, curses.color_pair(2))
        
        self.nav_win.refresh()
    
    def draw_search_bar(self):
        """Draw the search bar"""
        self.search_win.clear()
        self.search_win.box()
        
        if self.current_view == "search":
            prompt = "Search: "
            self.search_win.addstr(1, 2, prompt)
            
            if self.input_mode:
                self.search_win.attron(curses.color_pair(1))
                curses.curs_set(1)
            
            self.search_win.addstr(1, 2 + len(prompt), self.search_query)
            
            if not self.input_mode:
                curses.curs_set(0)
            
            instructions = "Enter: Search | Space: Play/Pause | q: Quit | /: Search mode"
        elif self.current_view == "history":
            instructions = "Click to play | l: Like | Space: Play/Pause | c: Clear history | q: Quit"
        else:  # liked
            instructions = "Click to play | l/d: Remove | Space: Play/Pause | c: Clear all | q: Quit"
        
        h, w = self.search_win.getmaxyx()
        if len(instructions) < w - 4:
            self.search_win.addstr(2, 2, instructions[:w-4], curses.color_pair(2))
        
        self.search_win.refresh()
    
    def draw_results(self):
        """Draw search results or history"""
        self.main_win.clear()
        
        if self.current_view == "search":
            self.draw_search_results()
        elif self.current_view == "history":
            self.draw_history()
        else:  # liked
            self.draw_liked()
        
        self.main_win.refresh()
    
    def draw_search_results(self):
        """Draw search results"""
        if not self.search_results:
            self.main_win.addstr(2, 2, "No results. Press '/' to search for music.", curses.color_pair(2))
            return
        
        h, w = self.main_win.getmaxyx()
        self.main_win.addstr(0, 2, "Search Results:", curses.color_pair(1) | curses.A_BOLD)
        
        for i, track in enumerate(self.search_results[:h-3]):
            self.draw_track_item(i, track, i + 2, show_like=True)
    
    def draw_history(self):
        """Draw listening history"""
        history = self.history_manager.get_history()
        if not history:
            self.main_win.addstr(2, 2, "No listening history yet. Play some music!", curses.color_pair(2))
            return
        
        h, w = self.main_win.getmaxyx()
        self.main_win.addstr(0, 2, f"Recently Played ({len(history)} tracks):", curses.color_pair(6) | curses.A_BOLD)
        
        for i, track in enumerate(history[:h-3]):
            if 'played_at' in track:
                played_time = time.strftime("%m/%d %H:%M", time.localtime(track['played_at']))
                track_display = track.copy()
                track_display['uploader'] = f"{track['uploader']} • {played_time}"
            else:
                track_display = track
            
            self.draw_track_item(i, track_display, i + 2, show_like=True)
    
    def draw_liked(self):
        """Draw liked songs"""
        liked = self.history_manager.get_liked()
        if not liked:
            self.main_win.addstr(2, 2, "No liked songs yet. Press 'l' to like tracks!", curses.color_pair(2))
            return
        
        h, w = self.main_win.getmaxyx()
        self.main_win.addstr(0, 2, f"Liked Songs ({len(liked)} tracks):", curses.color_pair(7) | curses.A_BOLD)
        
        for i, track in enumerate(liked[:h-3]):
            if 'played_at' in track:
                played_time = time.strftime("%m/%d %H:%M", time.localtime(track['played_at']))
                track_display = track.copy()
                track_display['uploader'] = f"{track['uploader']} • {played_time}"
            else:
                track_display = track
            
            self.draw_track_item(i, track_display, i + 2, show_like=False)
    
    def draw_track_item(self, index, track, y_pos, show_like=True):
        """Draw a single track item with optional like button"""
        h, w = self.main_win.getmaxyx()
        
        if y_pos >= h - 1:
            return
        
        # Determine color based on view
        if self.current_view == "liked":
            base_color = curses.color_pair(7)
        elif self.current_view == "history":
            base_color = curses.color_pair(6)
        else:
            base_color = curses.color_pair(2)
        
        # Highlight selected or playing track
        if index == self.selected_index:
            color = curses.color_pair(3) | curses.A_BOLD
        elif self.player.current_track and track['title'] in self.player.current_track:
            color = curses.color_pair(4) | curses.A_BOLD
        else:
            color = base_color
        
        # Format duration
        duration = track.get('duration', 0)
        duration_str = f"{int(duration) // 60:02d}:{int(duration) % 60:02d}" if duration else "??:??"
        
        # Format track info
        max_title_width = w - 25 if show_like else w - 20
        title = track['title'][:max_title_width] if len(track['title']) > max_title_width else track['title']
        uploader = track['uploader'][:15] if len(track['uploader']) > 15 else track['uploader']
        
        # Add like indicator if needed
        like_indicator = ""
        if show_like:
            is_liked = self.history_manager.is_liked(track)
            like_indicator = " ♥ " if is_liked else " ♡ "
            like_indicator = like_indicator.ljust(4)
        
        track_info = f"{title} - {uploader} [{duration_str}]{like_indicator}"
        
        # Highlight selection
        if index == self.selected_index:
            self.main_win.addstr(y_pos, 1, "►", curses.color_pair(1) | curses.A_BOLD)
        
        self.main_win.addstr(y_pos, 3, track_info[:w-5], color)
    
    def draw_player(self):
        """Draw the now playing bar"""
        self.player_win.clear()
        self.player_win.box()
        
        h, w = self.player_win.getmaxyx()
        
        if self.player.current_track:
            status = "⏸ PAUSED" if self.player.is_paused else "▶ PLAYING" if self.player.is_playing else "⏹ STOPPED"
            self.player_win.addstr(1, 2, status, curses.color_pair(4) | curses.A_BOLD)
            
            track_name = self.player.current_track
            max_width = w - 20
            if len(track_name) > max_width:
                track_name = track_name[:max_width-3] + "..."
            
            self.player_win.addstr(1, 15, track_name, curses.color_pair(2))
        else:
            self.player_win.addstr(1, 2, "♪ Ready to play music", curses.color_pair(2))
        
        self.player_win.refresh()
    
    def search_music(self, query: str):
        """Search for music"""
        if not query.strip():
            return
            
        self.search_results = []
        self.selected_index = 0
        
        self.main_win.clear()
        self.main_win.addstr(2, 2, f"Searching for '{query}'...", curses.color_pair(1))
        self.main_win.refresh()
        
        results = self.searcher.search(query)
        self.search_results = results
        
        if not results:
            self.main_win.clear()
            self.main_win.addstr(2, 2, "No results found. Try a different search.", curses.color_pair(5))
            self.main_win.refresh()
    
    def get_current_list(self):
        """Get current track list based on view"""
        if self.current_view == "search":
            return self.search_results
        elif self.current_view == "history":
            return self.history_manager.get_history()
        else:  # liked
            return self.history_manager.get_liked()
    
    def play_selected(self):
        """Play the selected track"""
        current_list = self.get_current_list()
        if not current_list or self.selected_index >= len(current_list):
            return
        
        track = current_list[self.selected_index]
        
        # Show loading
        h, w = self.main_win.getmaxyx()
        self.main_win.addstr(self.selected_index + 2, w - 15, "Loading...", curses.color_pair(1))
        self.main_win.refresh()
        
        # Add to history
        self.history_manager.add_track(track, to_history=True)
        
        # Play track
        self.player.play(track['url'], track['title'])
    
    def toggle_like(self):
        """Toggle like status for selected track"""
        current_list = self.get_current_list()
        if not current_list or self.selected_index >= len(current_list):
            return
        
        track = current_list[self.selected_index]
        
        if self.history_manager.is_liked(track):
            self.history_manager.remove_liked(track)
        else:
            self.history_manager.add_track(track, to_history=False, to_liked=True)
    
    def remove_selected_liked(self):
        """Remove currently selected track from liked songs"""
        if self.current_view != "liked":
            return
            
        current_list = self.get_current_list()
        if not current_list or self.selected_index >= len(current_list):
            return
        
        track = current_list[self.selected_index]
        self.history_manager.remove_liked(track)
        
        # Adjust selection if we removed the last item
        if self.selected_index >= len(self.history_manager.get_liked()):
            self.selected_index = max(0, self.selected_index - 1)
    
    def handle_mouse(self, mouse_event):
        """Handle mouse events"""
        _, x, y, _, button_state = mouse_event
        
        if button_state & curses.BUTTON1_CLICKED:
            # Check navigation tabs
            if 3 <= y <= 4:
                tab_ranges = [
                    (2, 10, "search"),
                    (12, 22, "history"),
                    (24, 38, "liked")
                ]
                for start, end, view in tab_ranges:
                    if start <= x <= end:
                        self.current_view = view
                        self.selected_index = 0
                        return
            
            # Check track list
            elif 8 <= y <= self.stdscr.getmaxyx()[0] - 4:
                track_index = y - 10
                current_list = self.get_current_list()
                if 0 <= track_index < len(current_list):
                    self.selected_index = track_index
                    
                    # Check if clicking on like button (if visible)
                    if self.current_view != "liked" and x >= self.stdscr.getmaxyx()[1] - 5:
                        self.toggle_like()
                    else:
                        self.play_selected()
    
    def handle_input(self):
        """Handle user input"""
        try:
            key = self.stdscr.getch()
            
            if key == curses.KEY_MOUSE:
                try:
                    mouse_event = curses.getmouse()
                    self.handle_mouse(mouse_event)
                except curses.error:
                    pass
                return True
            
            if self.input_mode:
                if key == ord('\n') or key == curses.KEY_ENTER:
                    self.input_mode = False
                    self.search_music(self.search_query)
                elif key == 27:  # ESC
                    self.input_mode = False
                elif key == curses.KEY_BACKSPACE or key == 127:
                    self.search_query = self.search_query[:-1]
                elif 32 <= key <= 126:
                    self.search_query += chr(key)
            else:
                if key == ord('q'):
                    return False
                elif key == ord('/') and self.current_view == "search":
                    self.input_mode = True
                    self.search_query = ""
                elif key == ord('1'):
                    self.current_view = "search"
                    self.selected_index = 0
                elif key == ord('2'):
                    self.current_view = "history"
                    self.selected_index = 0
                elif key == ord('3'):
                    self.current_view = "liked"
                    self.selected_index = 0
                elif key == ord('l'):
                    if self.current_view == "liked":
                        self.remove_selected_liked()
                    else:
                        self.toggle_like()
                elif key == ord('d'):  # Alternative delete key in liked view
                    if self.current_view == "liked":
                        self.remove_selected_liked()
                elif key == ord('c'):
                    if self.current_view == "history":
                        self.history_manager.clear_history()
                    elif self.current_view == "liked":
                        self.history_manager.clear_liked()
                    self.selected_index = 0
                elif key == curses.KEY_UP:
                    current_list = self.get_current_list()
                    if current_list:
                        self.selected_index = max(0, self.selected_index - 1)
                elif key == curses.KEY_DOWN:
                    current_list = self.get_current_list()
                    if current_list:
                        self.selected_index = min(len(current_list) - 1, self.selected_index + 1)
                elif key == ord('\n') or key == curses.KEY_ENTER:
                    self.play_selected()
                elif key == ord(' '):
                    self.player.pause()
                elif key == ord('s'):
                    self.player.stop()
            
            return True
        except KeyboardInterrupt:
            return False
    
    def run(self):
        """Main application loop"""
        self.stdscr.timeout(100)
        
        while True:
            self.draw_header()
            self.draw_navigation()
            self.draw_search_bar()
            self.draw_results()
            self.draw_player()
            
            if not self.handle_input():
                break
        
        self.player.stop()

def check_dependencies():
    """Check if required dependencies are installed"""
    missing = []
    
    try:
        subprocess.run(['mpv', '--version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        missing.append('mpv')
    
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        missing.append('yt-dlp')
    
    try:
        import pypresence
    except ImportError:
        missing.append('pypresence')
    
    if missing:
        print("Missing dependencies:")
        for dep in missing:
            print(f"  - {dep}")
        print("\nInstall with:")
        if 'mpv' in missing:
            print("  sudo apt install mpv")
        if 'yt-dlp' in missing:
            print("  pip install yt-dlp")
        if 'pypresence' in missing:
            print("  pip install pypresence")
        return False
    
    return True

def main():
    """Main entry point"""
    if not check_dependencies():
        sys.exit(1)
    
    try:
        curses.wrapper(lambda stdscr: SpotiTUI(stdscr).run())
    except KeyboardInterrupt:
        print("\nExiting SpotiTUI...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
