import array
import math
import random
import sys
import threading
import time

import pygame

pygame.mixer.pre_init(44100, -16, 1, 512)
pygame.init()
pygame.font.init()
try:
    pygame.mixer.init()
except pygame.error:
    pass
# --- Configuration & Constants ---
FPS = 60
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 700
GRID_COLS = 10
GRID_ROWS = 20
BLOCK_SIZE = 30

# Calculate grid offsets to center the board
GRID_X_OFFSET = (SCREEN_WIDTH - (GRID_COLS * BLOCK_SIZE)) // 2
GRID_Y_OFFSET = (SCREEN_HEIGHT - (GRID_ROWS * BLOCK_SIZE)) // 2

# Colors (Famicom/NES Inspired Palette)
COLOR_BG = (10, 10, 15)
COLOR_GRID_BG = (0, 0, 0)
COLOR_GRID_LINE = (40, 40, 50)
COLOR_TEXT = (255, 255, 255)
COLOR_TEXT_MUTED = (150, 150, 150)
COLOR_MENU_HOVER = (242, 169, 0)

# NES Tetris Piece Colors (Cycle based on level typically, using classic mappings here)
PIECE_COLORS = {
    'T': (74, 0, 242),
    'J': (0, 90, 242),
    'Z': (242, 0, 0),
    'O': (242, 169, 0),
    'S': (0, 218, 0),
    'L': (242, 100, 0),
    'I': (0, 218, 218)
}

# NES Tetris Original Frames-per-Drop Table (at 60 FPS)
NES_GRAVITY = {
    0: 48, 1: 43, 2: 38, 3: 33, 4: 28, 5: 23, 6: 18, 7: 13, 8: 8, 9: 6,
    10: 5, 11: 5, 12: 5, 13: 4, 14: 4, 15: 4, 16: 3, 17: 3, 18: 3
}  # Level 19+ is 2 frames, Level 29+ is 1 frame.

MAX_LEVEL = 256
LINES_PER_LEVEL = 10

# --- Korobeiniki (Tetris Type A) — gameplay BGM only, no external files ---
SR = 44100
NES_CPU = 1789773.0
TETRIS_BPM = 150
E8_MS = max(40, int(round(60000.0 / TETRIS_BPM / 2.0)))
DUTY_LEAD = 0.5
DUTY_HARM = 0.25

_NOTE = {
    "R": 0,
    "A2": 45, "E3": 52, "A3": 57, "B3": 59,
    "C4": 60, "D4": 62, "E4": 64, "G4": 67, "A4": 69, "B4": 71,
    "C5": 72, "D5": 74, "E5": 76, "F5": 78, "G5": 79, "A5": 81,
}


def _parse_score(rows: tuple[str, ...]) -> tuple[tuple[int, float], ...]:
    return tuple((_NOTE.get(n.split(":")[0], 0), float(n.split(":")[1])) for n in rows)


_KORO_LEAD = _parse_score(
    (
        "E5:2", "B4:1", "C5:1", "D5:2", "C5:1", "B4:1",
        "A4:2", "A4:1", "C5:1", "E5:2", "D5:1", "C5:1",
        "B4:3", "C5:1", "D5:2", "E5:2", "C5:2", "A4:2", "A4:2", "R:2",
        "D5:2", "F5:1", "A5:2", "G5:1", "F5:1",
        "E5:2", "E5:1", "C5:1", "E5:2", "D5:1", "C5:1",
        "B4:2", "B4:1", "C5:1", "D5:2", "E5:2", "C5:2", "A4:2", "A4:2", "R:2",
    )
)


def _harm_from_lead(lead: tuple[tuple[int, float], ...]) -> tuple[tuple[int, float], ...]:
    out: list[tuple[int, float]] = []
    for midi, dur in lead:
        if midi <= 0:
            out.append((0, dur))
            continue
        h = midi - 4
        while h < 55:
            h += 12
        while h > 79:
            h -= 12
        out.append((h, dur))
    return tuple(out)


def _bass_from_lead(lead: tuple[tuple[int, float], ...]) -> tuple[tuple[int, float], ...]:
    out: list[tuple[int, float]] = []
    toggle = 0
    for midi, dur in lead:
        if midi <= 0:
            out.append((0, dur))
            continue
        out.append((45 if toggle == 0 else 52, dur))
        toggle ^= 1
    return tuple(out)


_TYPE_A_HARM = _harm_from_lead(_KORO_LEAD)
_TYPE_A_BASS = _bass_from_lead(_KORO_LEAD)
_PERIOD: dict[int, int] = {}


def _e8_ms(eighths: float) -> int:
    return max(40, int(round(E8_MS * eighths)))


def _hz(midi: int) -> float:
    if midi <= 0:
        return 0.0
    if midi not in _PERIOD:
        f = 440.0 * (2.0 ** ((midi - 69) / 12.0))
        _PERIOD[midi] = max(0, int(round(NES_CPU / (16.0 * f) - 1.0)))
    return NES_CPU / (16.0 * (_PERIOD[midi] + 1))


def _make_tone(
    freq: float, ms: int, vol: float, *, duty: float = DUTY_LEAD, triangle: bool = False
) -> pygame.mixer.Sound | None:
    if ms < 1 or vol <= 0 or freq <= 0:
        return None
    n = max(1, int(SR * ms / 1000))
    amp = int(24000 * min(1.0, vol))
    buf = array.array("h", [0] * n)
    inc = freq / SR
    ph = 0.0
    for i in range(n):
        t = i / max(1, n)
        env = min(1.0, t / 0.03) if t < 0.03 else max(0.0, (1.0 - t) / 0.14) if t > 0.86 else 1.0
        ph += inc
        if ph >= 1.0:
            ph -= 1.0
        if triangle:
            tri = 2.0 * abs(2.0 * (ph - math.floor(ph + 0.5)) - 1.0) - 1.0
            s = int(amp * tri * env)
        else:
            s = int(amp * env) if ph < duty else int(-amp * env)
        buf[i] = s
    return pygame.mixer.Sound(buffer=buf)


def _play_tone(midi: int, ms: int, vol: float, *, duty: float = DUTY_LEAD, triangle: bool = False) -> None:
    play_ms = max(24, int(ms * 0.94)) if not triangle else ms
    snd = _make_tone(_hz(midi), play_ms, vol, duty=duty, triangle=triangle)
    if snd:
        snd.play()


def _zip_type_a() -> tuple[tuple[int, int, int, int], ...]:
    out: list[tuple[int, int, int, int]] = []
    for i, (m, me) in enumerate(_KORO_LEAD):
        h, _ = _TYPE_A_HARM[i]
        b, _ = _TYPE_A_BASS[i]
        out.append((m, h, b, _e8_ms(me)))
    return tuple(out)


TYPE_A_TRACK = _zip_type_a()

# NES-style level-up fanfare (embedded, no external files)
_LEVEL_FANFARE = (
    (60, 70), (64, 70), (67, 70), (72, 100), (76, 100), (79, 140), (84, 220),
)


def _play_level_fanfare() -> None:
    if not pygame.mixer.get_init():
        return

    def _run() -> None:
        for midi, ms in _LEVEL_FANFARE:
            _play_tone(midi, ms, 0.48, duty=DUTY_LEAD)
            _play_tone(max(55, midi - 12), ms, 0.22, duty=DUTY_HARM)
            time.sleep(ms / 1000.0 * 0.82)

    threading.Thread(target=_run, daemon=True).start()


class TetrisMusic:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not pygame.mixer.get_init():
            return
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def _play_track(self) -> None:
        lv, hv, bv = 0.42, 0.16, 0.26
        for mel, harm, bass, fr in TYPE_A_TRACK:
            if self._stop.is_set():
                return
            if mel > 0:
                _play_tone(mel, fr, lv, duty=DUTY_LEAD)
            if harm > 0 and mel > 0:
                _play_tone(harm, fr, hv, duty=DUTY_HARM)
            if bass > 0:
                _play_tone(bass, fr, bv, triangle=True)
            time.sleep(fr / 1000.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._play_track()


_bgm_player: TetrisMusic | None = None


def _get_bgm() -> TetrisMusic:
    global _bgm_player
    if _bgm_player is None:
        _bgm_player = TetrisMusic()
    return _bgm_player

# Tetromino Shapes (Represented in 4x4 or 3x3 matrices)
SHAPES = {
    'I': [[ [0,0,0,0], [1,1,1,1], [0,0,0,0], [0,0,0,0] ],
          [ [0,0,1,0], [0,0,1,0], [0,0,1,0], [0,0,1,0] ]],
    
    'O': [[ [1,1], [1,1] ]],
    
    'T': [[ [0,1,0], [1,1,1], [0,0,0] ],
          [ [0,1,0], [0,1,1], [0,1,0] ],
          [ [0,0,0], [1,1,1], [0,1,0] ],
          [ [0,1,0], [1,1,0], [0,1,0] ]],
    
    'J': [[ [1,0,0], [1,1,1], [0,0,0] ],
          [ [0,1,1], [0,1,0], [0,1,0] ],
          [ [0,0,0], [1,1,1], [0,0,1] ],
          [ [0,1,0], [0,1,0], [1,1,0] ]],
    
    'L': [[ [0,0,1], [1,1,1], [0,0,0] ],
          [ [0,1,0], [0,1,0], [0,1,1] ],
          [ [0,0,0], [1,1,1], [1,0,0] ],
          [ [1,1,0], [0,1,0], [0,1,0] ]],
    
    'Z': [[ [1,1,0], [0,1,1], [0,0,0] ],
          [ [0,0,1], [0,1,1], [0,1,0] ]],
    
    'S': [[ [0,1,1], [1,1,0], [0,0,0] ],
          [ [0,1,0], [0,1,1], [0,0,1] ]]
}

class Tetromino:
    def __init__(self, x, y, shape_type):
        self.x = x
        self.y = y
        self.type = shape_type
        self.rotations = SHAPES[shape_type]
        self.rotation_index = 0
        self.color = PIECE_COLORS[shape_type]

    @property
    def image(self):
        return self.rotations[self.rotation_index]

    def rotate(self):
        self.rotation_index = (self.rotation_index + 1) % len(self.rotations)

    def undo_rotate(self):
        self.rotation_index = (self.rotation_index - 1) % len(self.rotations)


class TetrisEngine:
    def __init__(self):
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Tetris - Famicom Edition")
        self.clock = pygame.time.Clock()
        
        # Fonts
        self.font_title = pygame.font.SysFont("Courier New", 50, bold=True)
        self.font_menu = pygame.font.SysFont("Courier New", 30, bold=True)
        self.font_ui = pygame.font.SysFont("Courier New", 24, bold=True)
        
        # Game State Engine
        self.state = "MAIN_MENU"
        self.menu_options = ["Play Game", "About", "Help", "Exit"]
        self.menu_index = 0
        
        # Reset game variables
        self.reset_game()

    def reset_game(self):
        self.grid = [[None for _ in range(GRID_COLS)] for _ in range(GRID_ROWS)]
        self.score = 0
        self.lines_cleared = 0
        self.level = 1
        self.game_over = False
        
        # NES DAS (Delayed Auto-Shift) emulation variables
        self.das_counter = 0
        self.das_delay = 16  # Frames before initial auto-repeat
        self.das_repeat = 6  # Frames between auto-repeats
        
        self.fall_frame_counter = 0
        self.current_piece = self.get_new_piece()
        self.next_piece = self.get_new_piece()

    def _start_bgm(self) -> None:
        _get_bgm().start()

    def _stop_bgm(self) -> None:
        _get_bgm().stop()

    def _return_to_menu(self) -> None:
        self._stop_bgm()
        self.state = "MAIN_MENU"

    def get_new_piece(self):
        shape_type = random.choice(list(SHAPES.keys()))
        # Center the piece horizontally at row 0 or -1 depending on layout
        return Tetromino(GRID_COLS // 2 - 1, 0, shape_type)

    def get_gravity_frames(self):
        idx = self.level - 1
        if idx in NES_GRAVITY:
            return NES_GRAVITY[idx]
        if idx >= 28:
            return 1
        return 2

    # --- Collision and Movement Mechanics ---
    def check_collision(self, piece, offset_x=0, offset_y=0, check_matrix=None):
        matrix = check_matrix if check_matrix is not None else piece.image
        for r, row in enumerate(matrix):
            for c, val in enumerate(row):
                if val:
                    new_x = piece.x + c + offset_x
                    new_y = piece.y + r + offset_y
                    if new_x < 0 or new_x >= GRID_COLS or new_y >= GRID_ROWS:
                        return True
                    if new_y >= 0 and self.grid[new_y][new_x] is not None:
                        return True
        return False

    def lock_piece(self):
        for r, row in enumerate(self.current_piece.image):
            for c, val in enumerate(row):
                if val:
                    grid_y = self.current_piece.y + r
                    grid_x = self.current_piece.x + c
                    if grid_y >= 0:
                        self.grid[grid_y][grid_x] = self.current_piece.color

        # NES-style placement score (4 minos per piece, scaled by level)
        self.score += 4 * self.level

        self.clear_lines()
        self.current_piece = self.next_piece
        self.next_piece = self.get_new_piece()
        
        # Game over condition
        if self.check_collision(self.current_piece):
            self.game_over = True

    def clear_lines(self):
        lines_to_clear = []
        for r in range(GRID_ROWS):
            if all(self.grid[r][c] is not None for c in range(GRID_COLS)):
                lines_to_clear.append(r)
                
        for r in lines_to_clear:
            del self.grid[r]
            self.grid.insert(0, [None for _ in range(GRID_COLS)])
            
        # NES Scoring Scheme
        num_lines = len(lines_to_clear)
        if num_lines == 1:
            self.score += 40 * self.level
        elif num_lines == 2:
            self.score += 100 * self.level
        elif num_lines == 3:
            self.score += 300 * self.level
        elif num_lines == 4:
            self.score += 1200 * self.level

        self.lines_cleared += num_lines
        new_level = min(MAX_LEVEL, self.lines_cleared // LINES_PER_LEVEL + 1)
        if new_level > self.level:
            self.level = new_level
            _play_level_fanfare()
        else:
            self.level = new_level

    # --- Core State Loops ---
    def run(self):
        while True:
            if self.state == "MAIN_MENU":
                self.handle_menu_events()
                self.draw_menu()
            elif self.state == "PLAYING":
                self.handle_game_events()
                if not self.game_over:
                    self.update_game_logic()
                self.draw_game()
            elif self.state in ["ABOUT", "HELP"]:
                self.handle_text_screen_events()
                self.draw_text_screen()
                
            self.clock.tick(FPS)

    # --- Event Handlers ---
    def handle_menu_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP:
                    self.menu_index = (self.menu_index - 1) % len(self.menu_options)
                elif event.key == pygame.K_DOWN:
                    self.menu_index = (self.menu_index + 1) % len(self.menu_options)
                elif event.key in [pygame.K_RETURN, pygame.K_SPACE]:
                    selection = self.menu_options[self.menu_index]
                    if selection == "Play Game":
                        self.reset_game()
                        self.state = "PLAYING"
                        self._start_bgm()
                    elif selection == "About":
                        self.state = "ABOUT"
                    elif selection == "Help":
                        self.state = "HELP"
                    elif selection == "Exit":
                        pygame.quit()
                        sys.exit()

    def handle_text_screen_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                # Any key returns to main menu
                self.state = "MAIN_MENU"

    def handle_game_events(self):
        keys = pygame.key.get_pressed()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.KEYDOWN:
                if self.game_over:
                    self._return_to_menu()
                    return

                if event.key == pygame.K_ESCAPE:
                    self._return_to_menu()
                    return
                elif event.key in [pygame.K_UP, pygame.K_x]: # X or Up to rotate
                    self.current_piece.rotate()
                    if self.check_collision(self.current_piece):
                        self.current_piece.undo_rotate()
                elif event.key == pygame.K_z: # Z to rotate counter-clockwise
                    for _ in range(3): self.current_piece.rotate()
                    if self.check_collision(self.current_piece):
                        self.current_piece.rotate()

        # Handle DAS (Left / Right Movement delays)
        if keys[pygame.K_LEFT]:
            if self.das_counter == 0 or self.das_counter >= self.das_delay:
                if not self.check_collision(self.current_piece, offset_x=-1):
                    self.current_piece.x -= 1
                if self.das_counter >= self.das_delay:
                    self.das_counter = self.das_delay - self.das_repeat
            self.das_counter += 1
        elif keys[pygame.K_RIGHT]:
            if self.das_counter == 0 or self.das_counter >= self.das_delay:
                if not self.check_collision(self.current_piece, offset_x=1):
                    self.current_piece.x += 1
                if self.das_counter >= self.das_delay:
                    self.das_counter = self.das_delay - self.das_repeat
            self.das_counter += 1
        else:
            self.das_counter = 0

    def update_game_logic(self):
        self.fall_frame_counter += 1
        keys = pygame.key.get_pressed()
        
        # Soft drop speed or natural gravity speed configuration
        drop_interval = 2 if keys[pygame.K_DOWN] else self.get_gravity_frames()
        
        if self.fall_frame_counter >= drop_interval:
            self.fall_frame_counter = 0
            if not self.check_collision(self.current_piece, offset_y=1):
                self.current_piece.y += 1
            else:
                self.lock_piece()

    # --- Render Engines ---
    def draw_menu(self):
        self.screen.fill(COLOR_BG)
        
        # Title Header ("duh duh duh main menu" stylized)
        title_surf = self.font_title.render("AC'S TETRIS", True, COLOR_TEXT)
        title_rect = title_surf.get_rect(center=(SCREEN_WIDTH // 2, 150))
        self.screen.blit(title_surf, title_rect)
        
        subtitle_surf = self.font_ui.render("FAMICOM CLONE", True, COLOR_TEXT_MUTED)
        subtitle_rect = subtitle_surf.get_rect(center=(SCREEN_WIDTH // 2, 210))
        self.screen.blit(subtitle_surf, subtitle_rect)
        
        # Draw Options
        for i, option in enumerate(self.menu_options):
            is_hovered = (i == self.menu_index)
            color = COLOR_MENU_HOVER if is_hovered else COLOR_TEXT
            text_str = f"> {option} <" if is_hovered else option
            
            opt_surf = self.font_menu.render(text_str, True, color)
            opt_rect = opt_surf.get_rect(center=(SCREEN_WIDTH // 2, 350 + i * 60))
            self.screen.blit(opt_surf, opt_rect)
            
        pygame.display.flip()

    def draw_text_screen(self):
        self.screen.fill(COLOR_BG)
        
        if self.state == "ABOUT":
            lines = [
                "ABOUT THIS CLONE",
                "",
                "This engine mirrors the Famicom/NES",
                "Tetris specifications closely.",
                "It locks framing systems to 60 FPS",
                "and accurately maps step-down frames",
                "per grid iteration.",
                "",
                "Press any key to go back."
            ]
        elif self.state == "HELP":
            lines = [
                "HOW TO PLAY",
                "",
                "LEFT / RIGHT Arrow : Move Piece",
                "DOWN Arrow         : Soft Drop",
                "UP Arrow / X Key   : Rotate Clockwise",
                "Z Key              : Rotate Counter-Clockwise",
                "ESCAPE             : Main Menu",
                "",
                "Press any key to go back."
            ]
            
        for i, line in enumerate(lines):
            surf = self.font_ui.render(line, True, COLOR_TEXT if i != 0 else COLOR_MENU_HOVER)
            rect = surf.get_rect(center=(SCREEN_WIDTH // 2, 150 + i * 40))
            self.screen.blit(surf, rect)
            
        pygame.display.flip()

    def draw_game(self):
        self.screen.fill(COLOR_BG)
        
        # Draw Active Grid Background Area
        pygame.draw.rect(self.screen, COLOR_GRID_BG, (GRID_X_OFFSET, GRID_Y_OFFSET, GRID_COLS * BLOCK_SIZE, GRID_ROWS * BLOCK_SIZE))
        
        # Render Locked Grid Blocks
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                color = self.grid[r][c]
                rect = pygame.Rect(GRID_X_OFFSET + c * BLOCK_SIZE, GRID_Y_OFFSET + r * BLOCK_SIZE, BLOCK_SIZE, BLOCK_SIZE)
                if color:
                    pygame.draw.rect(self.screen, color, rect)
                    pygame.draw.rect(self.screen, COLOR_GRID_BG, rect, 1) # inner block border
                else:
                    pygame.draw.rect(self.screen, COLOR_GRID_LINE, rect, 1)

        # Render Active Controlled Piece
        if not self.game_over and self.current_piece:
            for r, row in enumerate(self.current_piece.image):
                for c, val in enumerate(row):
                    if val:
                        pixel_x = GRID_X_OFFSET + (self.current_piece.x + c) * BLOCK_SIZE
                        pixel_y = GRID_Y_OFFSET + (self.current_piece.y + r) * BLOCK_SIZE
                        # Draw only if inside top bounding block space
                        if pixel_y >= GRID_Y_OFFSET:
                            rect = pygame.Rect(pixel_x, pixel_y, BLOCK_SIZE, BLOCK_SIZE)
                            pygame.draw.rect(self.screen, self.current_piece.color, rect)
                            pygame.draw.rect(self.screen, COLOR_GRID_BG, rect, 1)

        # Render UI Sidebar Information panels
        self.draw_ui_panel()

        if self.game_over:
            overlay = pygame.Surface((GRID_COLS * BLOCK_SIZE, GRID_ROWS * BLOCK_SIZE))
            overlay.set_alpha(180)
            overlay.fill((0, 0, 0))
            self.screen.blit(overlay, (GRID_X_OFFSET, GRID_Y_OFFSET))
            
            go_surf = self.font_menu.render("GAME OVER", True, (242, 0, 0))
            go_rect = go_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 20))
            sub_surf = self.font_ui.render("Press any key", True, COLOR_TEXT)
            sub_rect = sub_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 20))
            
            self.screen.blit(go_surf, go_rect)
            self.screen.blit(sub_surf, sub_rect)

        pygame.display.flip()

    def draw_ui_panel(self):
        # Score Panel
        score_lbl = self.font_ui.render("SCORE", True, COLOR_TEXT_MUTED)
        score_val = self.font_menu.render(f"{self.score:06d}", True, COLOR_TEXT)
        self.screen.blit(score_lbl, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 20))
        self.screen.blit(score_val, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 50))
        
        # Lines Panel
        lines_lbl = self.font_ui.render("LINES", True, COLOR_TEXT_MUTED)
        lines_val = self.font_menu.render(f"{self.lines_cleared:03d}", True, COLOR_TEXT)
        self.screen.blit(lines_lbl, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 130))
        self.screen.blit(lines_val, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 160))

        # Level Panel
        lvl_lbl = self.font_ui.render("LEVEL", True, COLOR_TEXT_MUTED)
        lvl_val = self.font_menu.render(f"{self.level:03d}", True, COLOR_TEXT)
        self.screen.blit(lvl_lbl, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 240))
        self.screen.blit(lvl_val, (GRID_X_OFFSET - 180, GRID_Y_OFFSET + 270))

        # Next Piece Panel
        next_lbl = self.font_ui.render("NEXT", True, COLOR_TEXT_MUTED)
        next_box_x = GRID_X_OFFSET + (GRID_COLS * BLOCK_SIZE) + 50
        next_box_y = GRID_Y_OFFSET + 20
        self.screen.blit(next_lbl, (next_box_x, next_box_y))
        
        # Render small preview matrix
        pygame.draw.rect(self.screen, COLOR_GRID_BG, (next_box_x, next_box_y + 30, 120, 120))
        pygame.draw.rect(self.screen, COLOR_GRID_LINE, (next_box_x, next_box_y + 30, 120, 120), 2)
        
        if self.next_piece:
            m = self.next_piece.rotations[0]
            for r, row in enumerate(m):
                for c, val in enumerate(row):
                    if val:
                        # Center layout configuration inside preview square
                        p_x = next_box_x + 20 + c * BLOCK_SIZE
                        p_y = next_box_y + 50 + r * BLOCK_SIZE
                        pygame.draw.rect(self.screen, self.next_piece.color, (p_x, p_y, BLOCK_SIZE, BLOCK_SIZE))
                        pygame.draw.rect(self.screen, COLOR_GRID_BG, (p_x, p_y, BLOCK_SIZE, BLOCK_SIZE), 1)


if __name__ == "__main__":
    try:
        game = TetrisEngine()
        game.run()
    finally:
        if _bgm_player is not None:
            _bgm_player.stop()
        pygame.quit()
