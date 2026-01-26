#!/usr/bin/env python3
"""
Interactive Git TUI - A simple terminal UI for git staging and committing
Requires: Python 3.6+ with curses (built-in on Unix systems)
"""

import curses
import subprocess
import sys
import tempfile
import os
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
    def __init__(self, stdscr):
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

        # Calculate visible area (reserve 2 lines for status + help)
        visible_height = height - 2

        # Group files by status
        staged_files = [f for f in self.files if f.staged]
        unstaged_files = [f for f in self.files if not f.staged and f.status != 'untracked']
        untracked_files = [f for f in self.files if f.status == 'untracked']

        # Build display lines: list of (text, attr, file_index_or_none)
        display_lines = []

        file_index = 0
        if staged_files:
            attr = curses.color_pair(1) | curses.A_BOLD if self.has_colors else curses.A_BOLD
            display_lines.append(("Changes to be committed:", attr, None))
            for f in staged_files:
                display_lines.append((f, None, file_index))
                file_index += 1
            display_lines.append(("", curses.A_NORMAL, None))

        if unstaged_files:
            attr = curses.color_pair(2) | curses.A_BOLD if self.has_colors else curses.A_BOLD
            display_lines.append(("Changes not staged for commit:", attr, None))
            for f in unstaged_files:
                display_lines.append((f, None, file_index))
                file_index += 1
            display_lines.append(("", curses.A_NORMAL, None))

        if untracked_files:
            attr = curses.color_pair(3) | curses.A_BOLD if self.has_colors else curses.A_BOLD
            display_lines.append(("Untracked files:", attr, None))
            for f in untracked_files:
                display_lines.append((f, None, file_index))
                file_index += 1

        if not self.files:
            display_lines.append(("No changes. Working directory clean.", curses.A_DIM, None))

        # Calculate scroll offset to keep cursor visible
        # Find which display line has the cursor
        cursor_display_line = 0
        for i, (content, attr, fidx) in enumerate(display_lines):
            if fidx == self.cursor_pos:
                cursor_display_line = i
                break

        # Adjust scroll offset
        if cursor_display_line < self.scroll_offset:
            self.scroll_offset = cursor_display_line
        elif cursor_display_line >= self.scroll_offset + visible_height:
            self.scroll_offset = cursor_display_line - visible_height + 1

        self.scroll_offset = max(0, min(self.scroll_offset, max(0, len(display_lines) - visible_height)))

        # Draw visible lines
        for row, (content, attr, fidx) in enumerate(display_lines[self.scroll_offset:]):
            if row >= visible_height:
                break

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

        # Draw help bar for diff view
        help_text = "Q Back  j/k Scroll  Space Stage/Unstage"
        attr = curses.color_pair(5) if self.has_colors else curses.A_REVERSE
        self._safe_addstr(height - 1, 0, help_text.ljust(width - 1), attr)

        self.stdscr.refresh()

    def _draw_status_bar(self):
        """Draw status message bar"""
        height, width = self.stdscr.getmaxyx()
        if self.status_message:
            self._safe_addstr(height - 2, 0, self.status_message, curses.A_BOLD)

    def _draw_help_bar(self):
        """Draw help bar at bottom"""
        height, width = self.stdscr.getmaxyx()
        help_text = "Q Quit  Space Stage/Unstage  D Diff  C Commit  R Refresh  j/k Navigate"
        attr = curses.color_pair(5) if self.has_colors else curses.A_REVERSE
        self._safe_addstr(height - 1, 0, help_text.ljust(width - 1), attr)

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

        if self.mode == 'list':
            return self._handle_list_input(key)
        elif self.mode == 'diff':
            return self._handle_diff_input(key)

        return True

    def _handle_list_input(self, key):
        """Handle input in list mode"""
        if key in (ord('q'), ord('Q')):
            return False

        elif key in (curses.KEY_UP, ord('k')):
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
                self.status_message = ""

        elif key in (curses.KEY_DOWN, ord('j')):
            if self.cursor_pos < len(self.files) - 1:
                self.cursor_pos += 1
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

        return True

    def _toggle_stage_current_file(self):
        """Toggle staging for the current file"""
        ordered = self._get_ordered_files()
        if self.cursor_pos >= len(ordered):
            return

        file = ordered[self.cursor_pos]
        current_path = file.path

        if file.staged:
            self.unstage_file(file)
        else:
            self.stage_file(file)

        self.parse_git_status()

        # Try to keep cursor on same file (it may have moved in the list)
        ordered = self._get_ordered_files()
        for i, f in enumerate(ordered):
            if f.path == current_path:
                self.cursor_pos = i
                break
        else:
            # File might be gone, clamp cursor
            self.cursor_pos = max(0, min(self.cursor_pos, len(ordered) - 1))

    def _stage_all(self):
        """Stage all unstaged and untracked files"""
        unstaged = [f for f in self.files if not f.staged]
        if not unstaged:
            self.status_message = "No files to stage"
            return

        for f in unstaged:
            self.run_git_command(['add', '--', f.path])

        self.parse_git_status()
        self.cursor_pos = 0
        self.status_message = f"Staged {len(unstaged)} file(s)"

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

        elif key in (curses.KEY_UP, ord('k')):
            self.diff_scroll = max(0, self.diff_scroll - 1)

        elif key in (curses.KEY_DOWN, ord('j')):
            max_scroll = max(0, len(self.diff_content) - viewable_lines)
            self.diff_scroll = min(self.diff_scroll + 1, max_scroll)

        elif key == ord(' '):
            self._toggle_stage_in_diff()

        elif key in (curses.KEY_PPAGE, ord('b')):  # Page up
            self.diff_scroll = max(0, self.diff_scroll - viewable_lines)

        elif key in (curses.KEY_NPAGE, ord('f')):  # Page down
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

        if not self.files:
            self.status_message = "No changes. Working directory clean."

        while True:
            if self.mode == 'list':
                self.draw_file_list()
            elif self.mode == 'diff':
                self.draw_diff_view()

            if not self.handle_input():
                break


def main(stdscr):
    """Main entry point for curses"""
    tui = GitTUI(stdscr)
    tui.run()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("\nExited.")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
