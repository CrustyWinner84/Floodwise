import tkinter as tk
import random
import math
import time

WIDTH, HEIGHT = 800, 480
FIELD_MARGIN = 40
PLAYER_RADIUS = 12
BALL_RADIUS = 8
FRICTION = 0.98
KICK_POWER = 8
COPILOT_DECISION_INTERVAL = 1000  # ms

# ---------------- Copilot component ----------------
class Copilot:
    """
    Small local Copilot that suggests actions and can control a teammate.
    Methods:
      suggest(player_pos, teammate_pos, ball_pos) -> str
      decide_action(player_pos, teammate_pos, ball_pos) -> dict
    """
    def __init__(self, randomness=0.2):
        self.randomness = randomness

    def distance(self, a, b):
        return math.hypot(a[0]-b[0], a[1]-b[1])

    def suggest(self, player_pos, teammate_pos, ball_pos):
        d_player_ball = self.distance(player_pos, ball_pos)
        d_teammate_ball = self.distance(teammate_pos, ball_pos)
        # Basic heuristics
        if d_player_ball < 40:
            if d_player_ball < 20:
                # close to ball
                if self.distance(player_pos, (WIDTH - FIELD_MARGIN, HEIGHT//2)) < 200:
                    return "Shoot: you're close to goal"
                if d_teammate_ball < 60:
                    return "Pass to teammate"
                return "Dribble forward"
            else:
                return "Move closer to the ball"
        else:
            if d_teammate_ball < d_player_ball:
                return "Support: move into space"
            return "Get open for a pass"

    def decide_action(self, player_pos, teammate_pos, ball_pos):
        """
        Return an action dict for the teammate when Copilot assist is enabled.
        Possible actions: move_to (x,y), receive_pass (x,y), go_for_ball
        """
        suggestion = self.suggest(player_pos, teammate_pos, ball_pos)
        # Add some randomness to avoid deterministic behavior
        if random.random() < self.randomness:
            suggestion = random.choice(["Dribble forward", "Pass to teammate", "Support: move into space", "Get open for a pass"])
        if suggestion.startswith("Pass"):
            # move to a receiving spot slightly ahead of player
            rx = (player_pos[0] + teammate_pos[0]) / 2 + random.randint(-30, 30)
            ry = (player_pos[1] + teammate_pos[1]) / 2 + random.randint(-20, 20)
            return {"type": "receive_pass", "target": (rx, ry)}
        if suggestion.startswith("Shoot"):
            return {"type": "shoot", "target": (WIDTH - FIELD_MARGIN, HEIGHT//2)}
        if suggestion.startswith("Move") or suggestion.startswith("Get") or suggestion.startswith("Support"):
            # move into open space (toward opponent goal but offset)
            tx = min(WIDTH - FIELD_MARGIN - 50, teammate_pos[0] + 80)
            ty = max(FIELD_MARGIN + 30, min(HEIGHT - FIELD_MARGIN - 30, teammate_pos[1] + random.randint(-60, 60)))
            return {"type": "move_to", "target": (tx, ty)}
        # default
        return {"type": "go_for_ball"}

# ---------------- Game objects and logic ----------------
class Game:
    def __init__(self, root):
        self.root = root
        self.canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, bg="#2e8b57")
        self.canvas.pack()
        self.init_ui()
        self.reset()
        self.copilot = Copilot()
        self.copilot_enabled = False
        self.last_copilot_time = 0
        self.root.bind("<KeyPress>", self.on_key)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.keys = set()
        self.update_loop()

    def init_ui(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="x")
        self.suggest_btn = tk.Button(frame, text="Suggest", command=self.show_suggestion)
        self.suggest_btn.pack(side="left", padx=6, pady=6)
        self.toggle_btn = tk.Button(frame, text="Enable Copilot Assist", command=self.toggle_copilot)
        self.toggle_btn.pack(side="left", padx=6)
        self.score_label = tk.Label(frame, text="Score 0 - 0", font=("Arial", 12))
        self.score_label.pack(side="right", padx=10)

    def reset(self):
        # positions: player (controlled), teammate (AI or copilot), ball
        self.player_pos = [FIELD_MARGIN + 80, HEIGHT//2]
        self.teammate_pos = [FIELD_MARGIN + 160, HEIGHT//2 + 60]
        self.ball_pos = [WIDTH//2, HEIGHT//2]
        self.ball_vel = [0.0, 0.0]
        self.player_score = 0
        self.opponent_score = 0
        self.draw_field()

    def draw_field(self):
        self.canvas.delete("all")
        # field rectangle
        self.canvas.create_rectangle(FIELD_MARGIN, FIELD_MARGIN, WIDTH-FIELD_MARGIN, HEIGHT-FIELD_MARGIN, fill="#3cb371", outline="white", width=2)
        # center line and circle
        self.canvas.create_line(WIDTH//2, FIELD_MARGIN, WIDTH//2, HEIGHT-FIELD_MARGIN, fill="white", width=2)
        self.canvas.create_oval(WIDTH//2 - 40, HEIGHT//2 - 40, WIDTH//2 + 40, HEIGHT//2 + 40, outline="white", width=2)
        # goals
        self.canvas.create_rectangle(WIDTH-FIELD_MARGIN, HEIGHT//2 - 60, WIDTH-FIELD_MARGIN+10, HEIGHT//2 + 60, fill="white", outline="")
        self.canvas.create_rectangle(FIELD_MARGIN-10, HEIGHT//2 - 60, FIELD_MARGIN, HEIGHT//2 + 60, fill="white", outline="")

    def draw_objects(self):
        # players
        px, py = self.player_pos
        tx, ty = self.teammate_pos
        bx, by = self.ball_pos
        # player
        self.canvas.create_oval(px-PLAYER_RADIUS, py-PLAYER_RADIUS, px+PLAYER_RADIUS, py+PLAYER_RADIUS, fill="blue", outline="black")
        self.canvas.create_text(px, py, text="P", fill="white")
        # teammate
        self.canvas.create_oval(tx-PLAYER_RADIUS, ty-PLAYER_RADIUS, tx+PLAYER_RADIUS, ty+PLAYER_RADIUS, fill="orange", outline="black")
        self.canvas.create_text(tx, ty, text="T", fill="white")
        # ball
        self.canvas.create_oval(bx-BALL_RADIUS, by-BALL_RADIUS, bx+BALL_RADIUS, by+BALL_RADIUS, fill="white", outline="black")

    def on_key(self, event):
        self.keys.add(event.keysym)

    def on_key_release(self, event):
        if event.keysym in self.keys:
            self.keys.remove(event.keysym)

    def handle_player_input(self):
        speed = 4
        if "Up" in self.keys or "w" in self.keys:
            self.player_pos[1] -= speed
        if "Down" in self.keys or "s" in self.keys:
            self.player_pos[1] += speed
        if "Left" in self.keys or "a" in self.keys:
            self.player_pos[0] -= speed
        if "Right" in self.keys or "d" in self.keys:
            self.player_pos[0] += speed
        # keep inside field
        self.player_pos[0] = max(FIELD_MARGIN+PLAYER_RADIUS, min(WIDTH-FIELD_MARGIN-PLAYER_RADIUS, self.player_pos[0]))
        self.player_pos[1] = max(FIELD_MARGIN+PLAYER_RADIUS, min(HEIGHT-FIELD_MARGIN-PLAYER_RADIUS, self.player_pos[1]))
        # kick with space
        if "space" in self.keys or "Space" in self.keys:
            self.attempt_kick(self.player_pos)

    def attempt_kick(self, kicker_pos):
        # if close to ball, kick it toward opponent goal
        if math.hypot(kicker_pos[0]-self.ball_pos[0], kicker_pos[1]-self.ball_pos[1]) < PLAYER_RADIUS + BALL_RADIUS + 6:
            # direction toward opponent goal center
            gx, gy = WIDTH - FIELD_MARGIN, HEIGHT//2
            dx, dy = gx - self.ball_pos[0], gy - self.ball_pos[1]
            dist = math.hypot(dx, dy) or 1
            self.ball_vel[0] = (dx/dist) * KICK_POWER
            self.ball_vel[1] = (dy/dist) * KICK_POWER

    def update_ball(self):
        # move ball
        self.ball_pos[0] += self.ball_vel[0]
        self.ball_pos[1] += self.ball_vel[1]
