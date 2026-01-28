#!/usr/bin/env python3
"""
Interactive Git TUI - A simple terminal UI for git staging and committing
Requires: Python 3.6+ with curses (built-in on Unix systems)
"""

import argparse
import curses
import fcntl
import subprocess
import sys
import tempfile
import time
import os
import select
from typing import List, Tuple, Optional


class GitFile:
    """Represents a file in the git status output"""
    def __init__(self, status: str, path: str, staged: bool):
        self.status = status
        self.path = path
        self.staged = staged

    def __eq__(self, other):
        if not isinstance(other, GitFile):
            return False
        return self.path == other.path and self.staged == other.staged

    def key(self) -> Tuple[str, bool]:
        """Unique key for this file entry"""
        return (self.path, self.staged)


class GitTUI:
    def __init__(self, stdscr, watch: bool = True):
        self.stdscr = stdscr
        self.cursor_pos = 0
        self.scroll_offset = 0
        self.files: List[GitFile] = []
        self.mode = 'list'  # 'list', 'diff', 'commit'
        self.diff_content: List[str] = []
        self.diff_scroll = 0
        self.status_message = ""
        self.has_colors = False
        self.repo_root: Optional[str] = None
        self.watch_enabled = watch
        self.watcher_proc: Optional[subprocess.Popen] = None
        self.last_refresh_time: float = 0
        self.events_during_cooldown = False

        self._init_curses()
        self._find_repo_root()

    def _find_repo_root(self):
        """Find the git repository root directory"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                self.repo_root = result.stdout.strip()
            else:
                self.status_message = "Error: Not a git repository"
        except Exception as e:
            self.status_message = f"Error finding repo: {e}"

    def _has_inotifywait(self) -> bool:
        """Check if inotifywait is available"""
        try:
            result = subprocess.run(
                ['which', 'inotifywait'],
                capture_output=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def _start_watcher(self):
        """Start inotifywait subprocess if available"""
        if not self.watch_enabled or not self.repo_root:
            return
        if not self._has_inotifywait():
            return

        try:
            self.watcher_proc = subprocess.Popen(
                ['inotifywait', '-r', '-m',
                 '-e', 'create',
                 '-e', 'modify',
                 '-e', 'delete',
                 '-e', 'move',
                 self.repo_root],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            # Make stdout non-blocking
            fd = self.watcher_proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:
            self.watcher_proc = None

    def _stop_watcher(self):
        """Stop inotifywait subprocess"""
        if self.watcher_proc:
            self.watcher_proc.terminate()
            try:
                self.watcher_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.watcher_proc.kill()
            self.watcher_proc = None

    def _check_watcher(self):
        """Check for inotifywait events and handle debouncing"""
        if not self.watcher_proc:
            return

        now = time.time()
        cooldown = 0.2  # 200ms
        in_cooldown = (now - self.last_refresh_time) < cooldown
        has_events = False

        # Check if there's data to read
        try:
            readable, _, _ = select.select([self.watcher_proc.stdout], [], [], 0)
            if readable:
                # Drain all available data
                try:
                    while self.watcher_proc.stdout.read(4096):
                        pass
                except (BlockingIOError, IOError):
                    pass
                has_events = True
        except Exception:
            pass

        if has_events:
            if not in_cooldown:
                # Refresh immediately
                self._refresh_status()
                self.last_refresh_time = time.time()
                self.events_during_cooldown = False
            else:
                # Remember we got events during cooldown
                self.events_during_cooldown = True
        elif self.events_during_cooldown and not in_cooldown:
            # Cooldown expired and we had events - do one final refresh
            self._refresh_status()
            self.last_refresh_time = time.time()
            self.events_during_cooldown = False

    def _init_curses(self):
        """Initialize curses settings"""
        curses.curs_set(0)  # Hide cursor
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)

        # Initialize colors if supported
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
            self.has_colors = True

    def run_git_command(self, args: List[str]) -> Tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr)"""
        try:
            result = subprocess.run(
                ['git'] + args,
                capture_output=True,
                text=True,
                cwd=self.repo_root  # Run from repo root so paths match
            )
            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return 1, "", str(e)

    def get_current_branch(self) -> str:
        """Get the current branch name"""
        returncode, stdout, stderr = self.run_git_command(['rev-parse', '--abbrev-ref', 'HEAD'])
        if returncode == 0:
            return stdout.strip()
        return ""

    def parse_git_status(self):
        """Parse git status --porcelain output"""
        returncode, stdout, stderr = self.run_git_command(['status', '--porcelain'])

        if returncode != 0:
            self.status_message = f"Error: {stderr}"
            return

        self.files = []

        if not stdout.strip():
            return

        for line in stdout.split('\n'):
            if not line or len(line) < 3:
                continue

            # Format: XY PATH (or XY ORIG -> PATH for renames)
            # X = staged status, Y = unstaged status
            x = line[0]
            y = line[1]
            path = line[3:]

            # Handle renames (format: "R  old -> new")
            if ' -> ' in path:
                path = path.split(' -> ')[-1]

            # Handle staged files (X is not space and not ?)
            if x not in (' ', '?'):
                status = self._status_char_to_name(x)
                self.files.append(GitFile(status, path, staged=True))

            # Handle unstaged/untracked files (Y is not space, or it's untracked ??)
            if y not in (' ',) or x == '?':
                if x == '?':
                    status = 'untracked'
                else:
                    status = self._status_char_to_name(y)
                self.files.append(GitFile(status, path, staged=False))

    def _status_char_to_name(self, char: str) -> str:
        """Convert git status character to human-readable name"""
        mapping = {
            'M': 'modified',
            'A': 'new file',
            'D': 'deleted',
            'R': 'renamed',
            'C': 'copied',
            'U': 'updated',
            '?': 'untracked',
        }
        return mapping.get(char, 'modified')

    def get_file_diff(self, file: GitFile) -> List[str]:
        """Get diff for a specific file"""
        if file.status == 'untracked':
            # For untracked files, show the file content
            try:
                full_path = os.path.join(self.repo_root, file.path) if self.repo_root else file.path
                with open(full_path, 'r', errors='replace') as f:
                    content = f.read()
                return [f"New file: {file.path}", ""] + content.split('\n')
            except Exception as e:
                return [f"Cannot read file: {file.path}", str(e)]

        if file.staged:
            args = ['diff', '--cached', '--', file.path]
        else:
            args = ['diff', '--', file.path]

        returncode, stdout, stderr = self.run_git_command(args)

        if returncode != 0:
            return [f"Error getting diff: {stderr}"]

        return stdout.split('\n') if stdout else [f"No changes for {file.path}"]

    def stage_file(self, file: GitFile):
        """Stage a file"""
        returncode, stdout, stderr = self.run_git_command(['add', '--', file.path])
        if returncode == 0:
            self.status_message = f"Staged: {file.path}"
        else:
            self.status_message = f"Error staging: {stderr}"

    def unstage_file(self, file: GitFile):
        """Unstage a file"""
        returncode, stdout, stderr = self.run_git_command(['reset', 'HEAD', '--', file.path])
        if returncode == 0:
            self.status_message = f"Unstaged: {file.path}"
        else:
            self.status_message = f"Error unstaging: {stderr}"

    def discard_changes(self, file: GitFile):
        """Discard changes in a file (restore to HEAD)"""
        returncode, stdout, stderr = self.run_git_command(['restore', '--', file.path])
        if returncode == 0:
            self.status_message = f"Discarded changes: {file.path}"
        else:
            self.status_message = f"Error discarding: {stderr}"

    def show_confirm_dialog(self, title: str, filename: str) -> bool:
        """Show a confirmation dialog, returns True if user confirms"""
        height, width = self.stdscr.getmaxyx()

        # Calculate dialog dimensions
        prompt = "(y/n)"
        max_content = max(len(title), len(filename), len(prompt))
        dialog_width = min(max_content + 6, width - 4)
        dialog_height = 5
        start_y = height // 2 - 2
        start_x = (width - dialog_width) // 2

        # Draw dialog box
        attr = curses.A_REVERSE
        for row in range(dialog_height):
            self._safe_addstr(start_y + row, start_x, " " * dialog_width, attr)

        # Draw title, filename, and prompt centered
        self._safe_addstr(start_y + 1, start_x + (dialog_width - len(title)) // 2, title, attr)
        display_name = filename if len(filename) < dialog_width - 4 else "..." + filename[-(dialog_width - 7):]
        self._safe_addstr(start_y + 2, start_x + (dialog_width - len(display_name)) // 2, display_name, attr)
        self._safe_addstr(start_y + 3, start_x + (dialog_width - len(prompt)) // 2, prompt, attr)

        self.stdscr.refresh()

        # Wait for y/n
        while True:
            key = self.stdscr.getch()
            if key in (ord('y'), ord('Y')):
                return True
            elif key in (ord('n'), ord('N'), 27):  # 27 = ESC
                return False

    def _get_ordered_files(self) -> List[GitFile]:
        """Get files in display order: staged, unstaged, untracked"""
        staged = [f for f in self.files if f.staged]
        unstaged = [f for f in self.files if not f.staged and f.status != 'untracked']
        untracked = [f for f in self.files if f.status == 'untracked']
        return staged + unstaged + untracked

    def _safe_addstr(self, row: int, col: int, text: str, attr=curses.A_NORMAL):
        """Safely add a string, handling edge cases"""
        height, width = self.stdscr.getmaxyx()
        if row < 0 or row >= height or col >= width:
            return

        # Truncate text to fit
        max_len = width - col - 1
        if max_len <= 0:
            return

        # Replace problematic characters
        safe_text = text[:max_len].replace('\t', '    ')

        try:
            self.stdscr.addstr(row, col, safe_text, attr)
        except curses.error:
            pass

    def draw_file_list(self):
        """Draw the main file list view"""
        self.stdscr.clear()
        height, width = self.stdscr.getmaxyx()

        # Draw branch name at top
        branch = self.get_current_branch()
        if branch:
            self._safe_addstr(0, 0, "On branch ")
            try:
                self.stdscr.addstr(branch, curses.A_REVERSE)
            except curses.error:
                pass

        # Calculate visible area (reserve 4 lines for branch + empty + status + help)
        visible_height = height - 4

        # Group files by status
        staged_files = [f for f in self.files if f.staged]
        unstaged_files = [f for f in self.files if not f.staged and f.status != 'untracked']
        untracked_files = [f for f in self.files if f.status == 'untracked']

        # Build display lines: list of (text, attr, file_index_or_none, section_start_line)
        display_lines = []

        file_index = 0

        attr = curses.color_pair(1) | curses.A_BOLD if self.has_colors else curses.A_BOLD
        section_start = len(display_lines)
        display_lines.append(("Changes to be committed:", attr, None, None))
        for f in staged_files:
            display_lines.append((f, None, file_index, section_start))
            file_index += 1
        display_lines.append(("", curses.A_NORMAL, None, None))

        attr = curses.color_pair(2) | curses.A_BOLD if self.has_colors else curses.A_BOLD
        section_start = len(display_lines)
        display_lines.append(("Changes not staged for commit:", attr, None, None))
        for f in unstaged_files:
            display_lines.append((f, None, file_index, section_start))
            file_index += 1
        display_lines.append(("", curses.A_NORMAL, None, None))

        attr = curses.color_pair(3) | curses.A_BOLD if self.has_colors else curses.A_BOLD
        section_start = len(display_lines)
        display_lines.append(("Untracked files:", attr, None, None))
        for f in untracked_files:
            display_lines.append((f, None, file_index, section_start))
            file_index += 1

        # Calculate scroll offset to keep cursor visible
        # Find which display line has the cursor and its section heading
        cursor_display_line = 0
        cursor_section_start = 0
        for i, (content, attr, fidx, section_start) in enumerate(display_lines):
            if fidx == self.cursor_pos:
                cursor_display_line = i
                cursor_section_start = section_start if section_start is not None else i
                break

        # Adjust scroll offset (ensure section heading is visible when scrolling up)
        if cursor_display_line < self.scroll_offset:
            self.scroll_offset = cursor_section_start
        elif cursor_display_line >= self.scroll_offset + visible_height:
            self.scroll_offset = cursor_display_line - visible_height + 1

        self.scroll_offset = max(0, min(self.scroll_offset, max(0, len(display_lines) - visible_height)))

        # Draw visible lines (starting at row 2, after branch line + empty line)
        for i, (content, attr, fidx, _) in enumerate(display_lines[self.scroll_offset:]):
            if i >= visible_height:
                break

            row = i + 2  # Offset for branch line + empty line
            if isinstance(content, GitFile):
                self._draw_file_line(row, fidx, content, width)
            else:
                self._safe_addstr(row, 0, content, attr if attr else curses.A_NORMAL)

        # Draw status bar and help
        self._draw_status_bar()
        self._draw_help_bar()

        self.stdscr.refresh()

    def _draw_file_line(self, row: int, file_index: int, file: GitFile, width: int):
        """Draw a single file line"""
        is_selected = file_index == self.cursor_pos
        cursor_marker = "> " if is_selected else "  "
        status_text = f"{file.status:10}"

        line = f"{cursor_marker}{status_text} {file.path}"

        attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
        self._safe_addstr(row, 0, line, attr)

    def draw_diff_view(self):
        """Draw the diff view"""
        self.stdscr.clear()
        height, width = self.stdscr.getmaxyx()
        visible_height = height - 3  # title + status + help

        # Draw title
        ordered = self._get_ordered_files()
        if self.cursor_pos < len(ordered):
            file = ordered[self.cursor_pos]
            title = f"Diff: {file.path} ({'staged' if file.staged else 'unstaged'})"
            attr = curses.color_pair(4) | curses.A_BOLD if self.has_colors else curses.A_BOLD
            self._safe_addstr(0, 0, title, attr)

        # Clamp scroll
        max_scroll = max(0, len(self.diff_content) - visible_height)
        self.diff_scroll = max(0, min(self.diff_scroll, max_scroll))

        # Draw diff content
        for i, line in enumerate(self.diff_content[self.diff_scroll:self.diff_scroll + visible_height]):
            row = i + 1

            # Color diff lines
            attr = curses.A_NORMAL
            if self.has_colors:
                if line.startswith('+') and not line.startswith('+++'):
                    attr = curses.color_pair(1)
                elif line.startswith('-') and not line.startswith('---'):
                    attr = curses.color_pair(2)
                elif line.startswith('@@'):
                    attr = curses.color_pair(4)

            self._safe_addstr(row, 0, line, attr)

        # Draw status bar
        self._draw_status_bar()

        # Draw help bar for diff view (nano-style)
        items = [("Q", "Back"), ("Space", "Stage"), ("PgUp/Dn", "Scroll")]
        col = 0
        for key, action in items:
            if col >= width - 1:
                break
            try:
                self.stdscr.addstr(height - 1, col, key, curses.A_REVERSE)
                col += len(key)
                self.stdscr.addstr(height - 1, col, " " + action + "  ")
                col += len(action) + 3
            except curses.error:
                pass

        self.stdscr.refresh()

    def _draw_status_bar(self):
        """Draw status message bar"""
        height, width = self.stdscr.getmaxyx()
        if self.status_message:
            self._safe_addstr(height - 2, 0, self.status_message, curses.A_BOLD)

    def _draw_help_bar(self):
        """Draw help bar at bottom (nano-style: keys reversed, actions normal)"""
        height, width = self.stdscr.getmaxyx()
        items = [("Q", "Quit"), ("Space", "Stage"), ("D", "Diff"), ("P", "Patch"), ("C", "Commit"), ("A", "Stage modified"), ("U", "Discard"), ("R", "Refresh")]
        col = 0
        for key, action in items:
            if col >= width - 1:
                break
            try:
                self.stdscr.addstr(height - 1, col, key, curses.A_REVERSE)
                col += len(key)
                self.stdscr.addstr(height - 1, col, " " + action + "  ")
                col += len(action) + 3
            except curses.error:
                pass

    def show_commit_dialog(self):
        """Show commit message input dialog"""
        # Check if there are staged files
        staged_files = [f for f in self.files if f.staged]
        if not staged_files:
            self.status_message = "No files staged for commit"
            return

        # Use external editor for commit message
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.txt', delete=False) as tf:
            temp_path = tf.name
            tf.write("\n")
            tf.write("# Please enter the commit message for your changes.\n")
            tf.write("# Lines starting with '#' will be ignored.\n")
            tf.write("#\n")
            tf.write("# Changes to be committed:\n")
            for file in staged_files:
                tf.write(f"#   {file.status}: {file.path}\n")

        # Temporarily end curses mode
        curses.endwin()

        # Open editor
        editor = os.environ.get('EDITOR', os.environ.get('VISUAL', 'nano'))
        try:
            subprocess.run([editor, temp_path])
        except Exception as e:
            os.unlink(temp_path)
            self.stdscr = curses.initscr()
            self._init_curses()
            self.status_message = f"Error opening editor: {e}"
            return

        # Read commit message
        try:
            with open(temp_path, 'r') as f:
                lines = [line.rstrip() for line in f.readlines() if not line.strip().startswith('#')]
                commit_msg = '\n'.join(lines).strip()
        except Exception:
            commit_msg = ""

        os.unlink(temp_path)

        # Reinitialize curses properly
        self.stdscr = curses.initscr()
        self._init_curses()

        if not commit_msg:
            self.status_message = "Commit cancelled (empty message)"
            return

        # Perform commit
        returncode, stdout, stderr = self.run_git_command(['commit', '-m', commit_msg])

        if returncode == 0:
            self.status_message = "Commit successful!"
            self.parse_git_status()
            self.cursor_pos = 0
        else:
            self.status_message = f"Commit failed: {stderr}"

    def handle_input(self):
        """Handle user input"""
        try:
            key = self.stdscr.getch()
        except KeyboardInterrupt:
            return False

        # Timeout (no input) - just continue
        if key == -1:
            return True

        if self.mode == 'list':
            return self._handle_list_input(key)
        elif self.mode == 'diff':
            return self._handle_diff_input(key)

        return True

    def _handle_list_input(self, key):
        """Handle input in list mode"""
        if key in (ord('q'), ord('Q')):
            return False

        elif key == curses.KEY_UP:
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
                self.status_message = ""

        elif key == curses.KEY_DOWN:
            if self.cursor_pos < len(self.files) - 1:
                self.cursor_pos += 1
                self.status_message = ""

        elif key == curses.KEY_PPAGE:
            height, _ = self.stdscr.getmaxyx()
            page_size = height - 4
            self.cursor_pos = max(0, self.cursor_pos - page_size)
            self.status_message = ""

        elif key == curses.KEY_NPAGE:
            height, _ = self.stdscr.getmaxyx()
            page_size = height - 4
            self.cursor_pos = min(len(self.files) - 1, self.cursor_pos + page_size)
            self.status_message = ""

        elif key == ord(' '):
            self._toggle_stage_current_file()

        elif key in (ord('d'), ord('D'), ord('\n'), curses.KEY_ENTER):
            # Show diff (also on Enter)
            if self.cursor_pos < len(self.files):
                ordered = self._get_ordered_files()
                if self.cursor_pos < len(ordered):
                    file = ordered[self.cursor_pos]
                    self.diff_content = self.get_file_diff(file)
                    self.diff_scroll = 0
                    self.mode = 'diff'
                    self.status_message = ""

        elif key in (ord('c'), ord('C')):
            self.show_commit_dialog()

        elif key in (ord('r'), ord('R')):
            self._refresh_status()

        elif key in (ord('a'), ord('A')):
            # Stage all files
            self._stage_all()

        elif key in (ord('p'), ord('P')):
            self._chunk_stage_current_file()

        elif key in (ord('u'), ord('U')):
            # Discard changes (restore to HEAD)
            self._discard_current_file()

        return True

    def _toggle_stage_current_file(self):
        """Toggle staging for the current file"""
        ordered = self._get_ordered_files()
        if self.cursor_pos >= len(ordered):
            return

        file = ordered[self.cursor_pos]

        # Remember the file below cursor to move there after operation
        next_file_key = None
        if self.cursor_pos + 1 < len(ordered):
            next_file_key = ordered[self.cursor_pos + 1].key()

        if file.staged:
            self.unstage_file(file)
        else:
            self.stage_file(file)

        self.parse_git_status()

        ordered = self._get_ordered_files()
        if len(ordered) == 0:
            self.cursor_pos = 0
            return

        # Find the next file's new position
        if next_file_key:
            for i, f in enumerate(ordered):
                if f.key() == next_file_key:
                    self.cursor_pos = i
                    return

        # No next file (was at end), move to new last position
        self.cursor_pos = min(self.cursor_pos, len(ordered) - 1)

    def _chunk_stage_current_file(self):
        """Run git add -p on the current file"""
        ordered = self._get_ordered_files()
        if self.cursor_pos >= len(ordered):
            return

        file = ordered[self.cursor_pos]

        if file.staged:
            self.status_message = "File is already staged (unstage first)"
            return
        if file.status == 'untracked':
            self.status_message = "Cannot chunk-stage untracked file"
            return

        curses.endwin()

        try:
            subprocess.run(['git', 'add', '-p', '--', file.path], cwd=self.repo_root)
        except Exception as e:
            self.stdscr = curses.initscr()
            self._init_curses()
            self.status_message = f"Error running git add -p: {e}"
            return

        self.stdscr = curses.initscr()
        self._init_curses()

        self.parse_git_status()
        self.cursor_pos = min(self.cursor_pos, max(0, len(self._get_ordered_files()) - 1))
        self.status_message = f"Chunk staging done: {file.path}"

    def _stage_all(self):
        """Stage all modified files (not untracked)"""
        modified = [f for f in self.files if not f.staged and f.status != 'untracked']
        if not modified:
            self.status_message = "No modified files to stage"
            return

        for f in modified:
            self.run_git_command(['add', '--', f.path])

        self.parse_git_status()
        self.cursor_pos = 0
        self.status_message = f"Staged {len(modified)} file(s)"

    def _discard_current_file(self):
        """Discard changes for the current file with confirmation"""
        ordered = self._get_ordered_files()
        if self.cursor_pos >= len(ordered):
            return

        file = ordered[self.cursor_pos]

        # Only works on unstaged, non-untracked files
        if file.staged:
            self.status_message = "Cannot discard staged changes (unstage first)"
            return
        if file.status == 'untracked':
            self.status_message = "Cannot discard untracked file"
            return

        # Show confirmation
        if not self.show_confirm_dialog("Discard changes?", file.path):
            self.status_message = "Cancelled"
            return

        self.discard_changes(file)
        self.parse_git_status()

        # Adjust cursor if needed
        ordered = self._get_ordered_files()
        if len(ordered) == 0:
            self.cursor_pos = 0
        else:
            self.cursor_pos = min(self.cursor_pos, len(ordered) - 1)

    def _refresh_status(self):
        """Refresh the git status"""
        old_files = self._get_ordered_files()
        old_key = old_files[self.cursor_pos].key() if self.cursor_pos < len(old_files) else None

        self.parse_git_status()

        # Try to preserve cursor position on same file
        if old_key:
            ordered = self._get_ordered_files()
            for i, f in enumerate(ordered):
                if f.key() == old_key:
                    self.cursor_pos = i
                    break
            else:
                self.cursor_pos = max(0, min(self.cursor_pos, len(ordered) - 1))
        else:
            self.cursor_pos = 0

        self.status_message = "Refreshed"

    def _handle_diff_input(self, key):
        """Handle input in diff mode"""
        height, _ = self.stdscr.getmaxyx()
        viewable_lines = height - 3

        if key in (ord('q'), ord('Q'), 27):  # 27 = ESC
            self.mode = 'list'
            self.status_message = ""

        elif key == curses.KEY_UP:
            self.diff_scroll = max(0, self.diff_scroll - 1)

        elif key == curses.KEY_DOWN:
            max_scroll = max(0, len(self.diff_content) - viewable_lines)
            self.diff_scroll = min(self.diff_scroll + 1, max_scroll)

        elif key == ord(' '):
            self._toggle_stage_in_diff()

        elif key == curses.KEY_PPAGE:
            self.diff_scroll = max(0, self.diff_scroll - viewable_lines)

        elif key == curses.KEY_NPAGE:
            max_scroll = max(0, len(self.diff_content) - viewable_lines)
            self.diff_scroll = min(self.diff_scroll + viewable_lines, max_scroll)

        return True

    def _toggle_stage_in_diff(self):
        """Toggle staging while in diff view"""
        ordered = self._get_ordered_files()
        if self.cursor_pos >= len(ordered):
            self.mode = 'list'
            return

        file = ordered[self.cursor_pos]
        current_path = file.path
        was_staged = file.staged

        if was_staged:
            self.unstage_file(file)
        else:
            self.stage_file(file)

        self.parse_git_status()

        # Find the same file in the new list (it will have toggled staged status)
        ordered = self._get_ordered_files()
        new_staged = not was_staged

        for i, f in enumerate(ordered):
            if f.path == current_path and f.staged == new_staged:
                self.cursor_pos = i
                self.diff_content = self.get_file_diff(f)
                self.diff_scroll = 0
                return

        # File might be gone or we can't find it - go back to list
        self.cursor_pos = max(0, min(self.cursor_pos, len(ordered) - 1))
        self.mode = 'list'

    def run(self):
        """Main run loop"""
        if not self.repo_root:
            # Show error and wait for keypress
            self.stdscr.addstr(0, 0, "Error: Not a git repository", curses.A_BOLD)
            self.stdscr.addstr(1, 0, "Press any key to exit.")
            self.stdscr.refresh()
            self.stdscr.getch()
            return

        self.parse_git_status()
        self._start_watcher()

        # Use timeout for getch so we can check for file changes
        if self.watcher_proc:
            self.stdscr.timeout(100)  # 100ms timeout

        if not self.files:
            self.status_message = "No changes. Working directory clean."

        try:
            while True:
                if self.mode == 'list':
                    self.draw_file_list()
                elif self.mode == 'diff':
                    self.draw_diff_view()

                if not self.handle_input():
                    break

                self._check_watcher()
        finally:
            self._stop_watcher()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Interactive Git TUI')
    parser.add_argument('--no-watch', action='store_true',
                        help='Disable automatic refresh via inotifywait')
    args = parser.parse_args()

    def run_tui(stdscr):
        tui = GitTUI(stdscr, watch=not args.no_watch)
        tui.run()

    try:
        curses.wrapper(run_tui)
    except KeyboardInterrupt:
        print("\nExited.")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
