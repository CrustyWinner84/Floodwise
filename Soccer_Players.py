import tkinter as tk
from tkinter import messagebox
import random

# ---------------- TRIVIA DATA ----------------
questions = [
    {"question": "Who is the the most World-renowned soccer player?",
     "choices": ["A) Ronaldo", "B) Mbappe", "C) Messi", "D) Neymar Jr."], "answer": "C"},
    {"question": "Who won the 2022 FIFA World Cup?",
     "choices": ["A) France", "B) Argentina", "C) Brazil", "D) Germany"], "answer": "B"},
    {"question": "Which soccer player has won the most Ballon D'or awards?",
     "choices": ["A) Ronaldo", "B) Yashin", "C) Messi", "D) Neymar Jr."], "answer": "C"},
    {"question": "Who is the only Goal Keeper to win a ballon d'or?",
     "choices": ["A) Zidane", "B) Cortouis", "C) Yashin", "D) Lloris"], "answer": "C"},
    {"question": "Who is the fastest soccer player?",
     "choices": ["A) Ronaldo", "B) Mbappe", "C) Messi", "D) Zlatan"], "answer": "B"}
]

random.shuffle(questions)
index = 0
score = 0

# ---------------- UI SETUP ----------------
root = tk.Tk()
root.title("Trivia Game")
root.geometry("900x600")

# Question label
question_label = tk.Label(root, text="", font=("Segoe UI", 26, "bold"), wraplength=800)
question_label.pack(pady=40)

# Buttons
buttons = []
for i in range(4):
    btn = tk.Button(root, text="", font=("Segoe UI", 18), width=25,
                    command=lambda i=i: check_answer(buttons[i].cget("text")))
    btn.pack(pady=10)
    buttons.append(btn)

# ---------------- FUNCTIONS ----------------
def load_question():
    global index
    q = questions[index]
    question_label.config(text=q["question"])
    for i, choice in enumerate(q["choices"]):
        buttons[i].config(text=choice, state=tk.NORMAL)

def check_answer(choice_text):
    global index, score
    correct = questions[index]["answer"]
    selected_letter = choice_text.split(")")[0]

    if selected_letter == correct:
        score += 1
        messagebox.showinfo("Correct!", "Nice job!")
    else:
        correct_display = next((c for c in questions[index]["choices"]
                                if c.startswith(correct + ")")), correct)
        messagebox.showinfo("Wrong!", f"The correct answer was: {correct_display}")

    index += 1
    if index < len(questions):
        load_question()
    else:
        messagebox.showinfo("Game Over", f"You scored {score} out of {len(questions)}")
        root.destroy()

# Load first question
load_question()

root.mainloop()
