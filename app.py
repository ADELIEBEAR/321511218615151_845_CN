import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog, font
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
# [수정] 최신 버전 호환성 확보
try:
    from ttkbootstrap.widgets.scrolled import ScrolledFrame
except ImportError:
    from ttkbootstrap.scrolled import ScrolledFrame

from tkinterdnd2 import DND_FILES, TkinterDnD
import google.generativeai as genai
from PIL import Image, ImageTk
import io
import os
import threading
import datetime
import re
import subprocess
import webbrowser
import requests
import json
import time
from youtube_search import YoutubeSearch
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import platform

# ==========================================
# 0. 환경 설정
# ==========================================
KEY_FILE = "api_key.txt"
GAMMA_KEY_FILE = "gamma_api_key.txt"
OUTPUT_DIR = "정석의_결과물"
FIXED_FILE = "fixed.txt"

DEFAULT_FIXED = """[고정 페르소나: 정석]
나는 대한민국 1타 경제 유튜버다.
말투는 냉철하고 분석적이며, '형님들', '가즈아' 같은 저급한 은어는 절대 쓰지 않는다.
대신 '유동성', '매크로', '세력의 의도' 등 전문적인 용어를 사용하여 신뢰감을 준다.
항상 데이터와 팩트를 기반으로 말하며, 결론은 시청자의 행동(구독/신청)을 강력하게 유도한다."""

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)
if not os.path.exists(FIXED_FILE):
    with open(FIXED_FILE, "w", encoding="utf-8") as f:
        f.write(DEFAULT_FIXED)


# ==========================================
# 1. 유틸리티 & 로딩 클래스
# ==========================================
class LoadingOverlay(tk.Toplevel):
    def __init__(self, parent, text="AI 작업 중..."):
        super().__init__(parent)
        self.transient(parent)
        self.overrideredirect(True)

        w, h = 400, 130
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (w // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.configure(bg="#2b2b2b", highlightbackground="#00d2d3", highlightthickness=2)

        ttk.Label(self, text=f"⏳ {text}", font=("맑은 고딕", 11, "bold"), bootstyle="inverse-dark").pack(
            pady=25
        )

        # [수정] Floodgauge -> Progressbar (안정성)
        self.pb = ttk.Progressbar(self, mode="indeterminate", bootstyle="success-striped", length=300)
        self.pb.pack(pady=5)
        self.pb.start(10)

    def close(self):
        self.destroy()


def get_api_key(file):
    if os.path.exists(file):
        with open(file, "r") as f:
            return f.read().strip()
    return None


def save_keys_first_time(root_win):
    def save():
        k1 = ent_k1.get().strip()
        k2 = ent_k2.get().strip()
        if k1:
            with open(KEY_FILE, "w") as f:
                f.write(k1)
            if k2:
                with open(GAMMA_KEY_FILE, "w") as f:
                    f.write(k2)
            win.destroy()
            root_win.deiconify()

    root_win.withdraw()
    win = tk.Toplevel(root_win)
    win.title("API 설정")
    win.geometry("500x300")
    ttk.Label(win, text="1. Google Gemini Key (필수)").pack(pady=5)
    ent_k1 = ttk.Entry(win, width=50, show="*"); ent_k1.pack(pady=5)
    ttk.Label(win, text="2. Gamma App Key (선택)").pack(pady=5)
    ent_k2 = ttk.Entry(win, width=50, show="*"); ent_k2.pack(pady=5)
    ttk.Button(win, text="저장 및 시작", command=save).pack(pady=20)
    win.wait_window()


def log(widget, msg, level="info"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    icon = "✅" if level == "info" else "❌"
    try:
        widget.configure(state="normal")
        widget.insert(tk.END, f"[{ts}] {icon} {msg}\n")
        widget.see(tk.END)
        widget.configure(state="disabled")
    except Exception:
        pass


def clean_filename(text):
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()[:30]


def open_folder():
    abs_path = os.path.abspath(OUTPUT_DIR)
    if hasattr(os, "startfile"):
        os.startfile(abs_path)
    else:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", abs_path], check=False)
        else:
            subprocess.run(["xdg-open", abs_path], check=False)


# ==========================================
# [2] 메인 애플리케이션
# ==========================================
class JeongseokApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("🍌 정석의 AI 스튜디오 : The Final Verified")
        self.geometry("1800x1000")
        self.style = ttk.Style(theme="cyborg")

        # [수정] 변수 초기화 (가장 먼저 실행)
        self.news_items = []
        self.yt_items = []
        self.uploaded_files = []
        self.current_thumb = None  # 초기화 필수
        self.ref_widgets = []

        self.check_keys()
        self.setup_ui()
        self.load_persona()

    def check_keys(self):
        if not get_api_key(KEY_FILE):
            save_keys_first_time(self)
        genai.configure(api_key=get_api_key(KEY_FILE))

    def load_persona(self):
        if os.path.exists(FIXED_FILE):
            with open(FIXED_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                self.txt_persona.configure(state="normal")
                self.txt_persona.delete("1.0", tk.END)
                self.txt_persona.insert("1.0", content)
                self.txt_persona.configure(state="disabled")

    # --- UI 구성 ---
    def setup_ui(self):
        self.paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        self.paned.pack(fill=BOTH, expand=True, padx=10, pady=10)

        # [1] 좌측
        self.frame_left = ttk.Frame(self.paned)
        self.paned.add(self.frame_left, weight=1)
        self.setup_left_panel()

        # [2] 중앙
        self.frame_center = ttk.Frame(self.paned)
        self.paned.add(self.frame_center, weight=2)
        self.setup_center_panel()

        # [3] 우측
        self.frame_right = ttk.Frame(self.paned)
        self.paned.add(self.frame_right, weight=1)
        self.setup_right_panel()

        # 하단 로그
        self.frame_log = ttk.Labelframe(self, text="System Log", height=100, bootstyle="secondary")
        self.frame_log.pack(fill=X, side=BOTTOM, padx=10, pady=5)
        self.txt_log = scrolledtext.ScrolledText(
            self.frame_log, height=5, state="disabled", bg="#111", fg="#0f0", font=("Consolas", 9)
        )
        self.txt_log.pack(fill=BOTH, expand=True)

    def setup_left_panel(self):
        nb = ttk.Notebook(self.frame_left)
        nb.pack(fill=BOTH, expand=True)

        # 뉴스
        t_news = ttk.Frame(nb); nb.add(t_news, text="📰 뉴스룸")
        f_n_ctrl = ttk.Frame(t_news); f_n_ctrl.pack(fill=X, pady=5)
        self.ent_news = ttk.Entry(f_n_ctrl); self.ent_news.pack(side=LEFT, fill=X, expand=True)
        self.ent_news.insert(0, "경제")
        self.ent_news.bind("<Return>", lambda e: self.fetch_news())
        ttk.Button(f_n_ctrl, text="검색", command=self.fetch_news, bootstyle="info-outline").pack(side=RIGHT, padx=2)
        self.scroll_news = ScrolledFrame(t_news, autohide=True); self.scroll_news.pack(fill=BOTH, expand=True, pady=5)

        # 유튜브
        t_yt = ttk.Frame(nb); nb.add(t_yt, text="📺 유튜브")
        f_y_ctrl = ttk.Frame(t_yt); f_y_ctrl.pack(fill=X, pady=5)
        self.ent_yt = ttk.Entry(f_y_ctrl); self.ent_yt.pack(side=LEFT, fill=X, expand=True)
        self.ent_yt.insert(0, "비트코인 전망")
        self.ent_yt.bind("<Return>", lambda e: self.fetch_youtube())
        ttk.Button(f_y_ctrl, text="검색", command=self.fetch_youtube, bootstyle="danger-outline").pack(side=RIGHT, padx=2)
        self.scroll_yt = ScrolledFrame(t_yt, autohide=True); self.scroll_yt.pack(fill=BOTH, expand=True, pady=5)

    def setup_center_panel(self):
        # 페르소나
        f_p = ttk.Labelframe(self.frame_center, text="🔒 고정 페르소나 (Fixed)", padding=5)
        f_p.pack(fill=X, pady=5)
        self.txt_persona = scrolledtext.ScrolledText(f_p, height=3, bg="#222", fg="cyan", state="disabled")
        self.txt_persona.pack(fill=X)

        # 참고 자료
        f_ref = ttk.Labelframe(self.frame_center, text="참고 자료 (3개 Mix)", padding=5)
        f_ref.pack(fill=X, pady=5)
        nb_ref = ttk.Notebook(f_ref, height=100)
        nb_ref.pack(fill=X)
        for i in range(3):
            fr = ttk.Frame(nb_ref); nb_ref.add(fr, text=f"참고 {i+1}")
            t = scrolledtext.ScrolledText(fr, height=4, font=("맑은 고딕", 9)); t.pack(fill=BOTH, expand=True)
            self.ref_widgets.append(t)

        # 작업 모드
        nb_work = ttk.Notebook(self.frame_center, bootstyle="primary")
        nb_work.pack(fill=X, pady=10)

        # A. 텍스트
        t_text = ttk.Frame(nb_work); nb_work.add(t_text, text=" 📝 주제/뉴스 믹스 ")
        self.ent_topic = ttk.Entry(t_text); self.ent_topic.pack(fill=X, padx=5, pady=5)
        ttk.Button(t_text, text="▶ 대본 생성 (뉴스 크롤링 포함)", command=self.gen_script_text, bootstyle="success").pack(
            fill=X, padx=5, pady=10
        )

        # B. 슬라이드
        t_vis = ttk.Frame(nb_work); nb_work.add(t_vis, text=" 🖼️ 슬라이드 분석 ")
        self.lbl_drop = tk.Label(t_vis, text="[이미지 드래그]", bg="#333", fg="#aaa", height=3, relief="sunken")
        self.lbl_drop.pack(fill=X, padx=5, pady=5)
        self.lbl_drop.drop_target_register(DND_FILES); self.lbl_drop.dnd_bind('<<Drop>>', self.on_drop)
        self.list_slides = tk.Listbox(t_vis, height=3, bg="#222", fg="white", bd=0); self.list_slides.pack(fill=X, padx=5)
        ttk.Button(t_vis, text="▶ 슬라이드 분석 대본 생성", command=self.gen_script_vision, bootstyle="info").pack(
            fill=X, padx=5, pady=10
        )

        # C. AI 채팅
        t_chat = ttk.Frame(nb_work); nb_work.add(t_chat, text=" 💬 AI 채팅 ")
        self.txt_chat = scrolledtext.ScrolledText(t_chat, height=5, state="disabled"); self.txt_chat.pack(fill=BOTH, padx=5)
        f_ci = ttk.Frame(t_chat); f_ci.pack(fill=X, padx=5, pady=5)
        self.ent_chat = ttk.Entry(f_ci); self.ent_chat.pack(side=LEFT, fill=X, expand=True)
        self.ent_chat.bind("<Return>", lambda e: self.send_chat())
        ttk.Button(f_ci, text="전송", command=self.send_chat).pack(side=RIGHT)

        # 결과창 & 감마
        ttk.Label(self.frame_center, text="📜 최종 대본", font=("bold", 10)).pack(anchor="w")
        self.txt_result = scrolledtext.ScrolledText(self.frame_center, font=("맑은 고딕", 11))
        self.txt_result.pack(fill=BOTH, expand=True, pady=5)
        ttk.Button(self.frame_center, text="⚡ Gamma PPT 생성 (API)", command=self.run_gamma, bootstyle="warning-outline").pack(
            fill=X, pady=5
        )

    def setup_right_panel(self):
        ttk.Label(self.frame_right, text="🎨 디자인 스튜디오", font=("Impact", 15), bootstyle="danger").pack(pady=10)

        ttk.Button(self.frame_right, text="✨ 아이디어 추천", command=self.recommend_idea, bootstyle="warning-outline").pack(
            fill=X, padx=5
        )
        self.list_ideas = tk.Listbox(self.frame_right, height=3, bg="#222", fg="white", bd=0)
        self.list_ideas.pack(fill=X, padx=5, pady=5)
        self.list_ideas.bind("<<ListboxSelect>>", self.on_idea_select)

        f_form = ttk.Labelframe(self.frame_right, text="설정", padding=10)
        f_form.pack(fill=X, padx=5)

        self.create_labeled_entry(f_form, "1. 메인 제목", "ent_main")
        self.create_labeled_entry(f_form, "2. 서브 문구", "ent_sub")
        self.create_labeled_entry(f_form, "3. 그림 묘사", "txt_scene", is_text=True)
        self.create_labeled_entry(f_form, "4. 추가 키워드", "ent_key")

        self.cb_style = ttk.Combobox(f_form, values=["초고화질 실사", "3D 렌더링", "웹툰", "사이버펑크"])
        self.cb_style.current(0); self.cb_style.pack(fill=X, pady=5)

        ttk.Button(self.frame_right, text="🖼️ 썸네일 생성", command=self.gen_thumb, bootstyle="danger").pack(fill=X, padx=5, pady=10)

        self.lbl_preview = tk.Label(self.frame_right, text="Preview", bg="black", fg="gray")
        self.lbl_preview.pack(fill=BOTH, expand=True, padx=5)
        self.lbl_preview.bind('<Configure>', self.resize_preview)

        ttk.Button(self.frame_right, text="📂 폴더 열기", command=open_folder, bootstyle="secondary").pack(fill=X, padx=5, pady=5)

    def create_labeled_entry(self, parent, text, var_name, is_text=False):
        ttk.Label(parent, text=text, font=("size", 9), foreground="cyan").pack(anchor="w")
        if is_text:
            w = tk.Text(parent, height=3, bg="#333", fg="white", font=("맑은 고딕", 10)); w.pack(fill=X)
        else:
            w = ttk.Entry(parent); w.pack(fill=X)
        setattr(self, var_name, w)

    # ==========================================
    # [3] 로직
    # ==========================================
    def fetch_news(self):
        query = self.ent_news.get()
        self.popup = LoadingOverlay(self, "뉴스 수집 중...")
        threading.Thread(target=self.thread_news, args=(query, self.popup), daemon=True).start()

    def thread_news(self, query, loading):
        try:
            url = f"https://news.google.com/rss/search?q={query}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
            root = ET.fromstring(res.content)

            self.news_items = []
            for w in self.scroll_news.container.winfo_children():
                w.destroy()

            for item in root.findall(".//item")[:20]:
                t = item.find("title").text; l = item.find("link").text
                var = tk.BooleanVar()
                chk = ttk.Checkbutton(self.scroll_news.container, text=t, variable=var, bootstyle="round-toggle")
                chk.pack(anchor="w", fill=X, pady=2)
                chk.bind("<Button-3>", lambda e, l=l: webbrowser.open(l))
                self.news_items.append({"title": t, "link": l, "var": var})
            self.after(0, lambda: log(self.txt_log, "뉴스 로드 완료"))
        except Exception as e:
            self.after(0, lambda: log(self.txt_log, f"뉴스 에러: {e}", "error"))
        finally:
            self.after(0, loading.close)

    def fetch_youtube(self):
        query = self.ent_yt.get()
        self.popup = LoadingOverlay(self, "유튜브 분석 중...")
        threading.Thread(target=self.thread_yt, args=(query, self.popup), daemon=True).start()

    def thread_yt(self, query, loading):
        try:
            res = YoutubeSearch(query, max_results=20).to_dict()
            self.yt_items = []
            for w in self.scroll_yt.container.winfo_children():
                w.destroy()
            for v in res:
                t = v['title']; l = "https://www.youtube.com" + v['url_suffix']; v_cnt = v['views']
                var = tk.BooleanVar()
                chk = ttk.Checkbutton(self.scroll_yt.container, text=f"🔥 [{v_cnt}] {t}", variable=var, bootstyle="round-toggle")
                chk.pack(anchor="w", fill=X, pady=2)
                chk.bind("<Button-3>", lambda e, l=l: webbrowser.open(l))
                self.yt_items.append({"title": t, "link": l, "var": var})
        except Exception as e:
            self.after(0, lambda: log(self.txt_log, f"유튜브 에러: {e}", "error"))
        finally:
            self.after(0, loading.close)

    def on_drop(self, event):
        self.uploaded_files = self.tk.splitlist(event.data)
        self.list_slides.delete(0, tk.END)
        for f in self.uploaded_files:
            self.list_slides.insert(tk.END, os.path.basename(f))
        self.lbl_drop.config(text=f"✅ {len(self.uploaded_files)}장 로드됨")

    def gen_script_text(self):
        sel_news = [i for i in self.news_items if i['var'].get()]
        sel_yt = [i for i in self.yt_items if i['var'].get()]
        topic = self.ent_topic.get()

        if not sel_news and not sel_yt and not topic:
            return messagebox.showwarning("!", "재료가 없습니다.")

        self.popup = LoadingOverlay(self, "뉴스 본문 딥 크롤링 중...")
        threading.Thread(target=self.thread_gen_text, args=(topic, sel_news, self.popup), daemon=True).start()

    def thread_gen_text(self, topic, sel_news, loading):
        contents = []
        for n in sel_news[:3]:
            try:
                h = {'User-Agent': 'Mozilla/5.0'}
                soup = BeautifulSoup(requests.get(n['link'], headers=h, timeout=3).content, 'html.parser')
                txt = soup.find('body').get_text(separator=' ', strip=True)[:1500]
                contents.append(f"기사: {n['title']}\n내용: {txt}")
            except Exception:
                pass
        full_prompt = f"주제: {topic}\n" + "\n".join(contents)
        self.call_ai(full_prompt, loading)

    def gen_script_vision(self):
        if not self.uploaded_files:
            return messagebox.showwarning("!", "이미지 없음")
        self.popup = LoadingOverlay(self, "슬라이드 시각 분석 중...")
        threading.Thread(target=self.thread_vision, args=(self.popup,), daemon=True).start()

    def thread_vision(self, loading):
        imgs = [Image.open(p) for p in self.uploaded_files]
        inp = ["이 슬라이드들을 분석해서 발표자가 말할 대본을 작성해. 순서대로 1, 2, 3..."] + imgs
        self.call_ai(inp, loading, is_vision=True)

    def call_ai(self, inp, loading, is_vision=False):
        try:
            persona = self.txt_persona.get("1.0", tk.END)
            refs = [w.get("1.0", tk.END).strip() for w in self.ref_widgets]

            prompt = f"""
            [ROLE] {persona}
            [REFS] {refs}
            [TASK] 7000자 내외 심층 대본 작성.
            [AUTO PLAN] 끝에 [PLAN] 태그 후 썸네일 기획(Main|Sub|Prompt)
            """
            model = genai.GenerativeModel('models/gemini-2.0-flash')
            final = [prompt]
            if is_vision:
                final += inp
            else:
                final.append(inp)

            res = model.generate_content(final)
            self.after(0, lambda: self.update_result(res.text))
        except Exception as e:
            self.after(0, lambda: log(self.txt_log, f"AI Error: {e}", "error"))
        finally:
            self.after(0, loading.close)

    def update_result(self, text):
        self.txt_result.delete("1.0", tk.END); self.txt_result.insert("1.0", text)
        if "[PLAN]" in text:
            try:
                plan = text.split("[PLAN]")[1].strip().split('\n')
                for l in plan:
                    if "|" in l:
                        p = l.split("|")
                        if len(p) >= 3:
                            self.ent_main.delete(0, tk.END); self.ent_main.insert(0, p[0].strip())
                            self.ent_sub.delete(0, tk.END); self.ent_sub.insert(0, p[1].strip())
                            self.txt_scene.delete("1.0", tk.END); self.txt_scene.insert("1.0", p[2].strip())
                            break
            except Exception:
                pass
        log(self.txt_log, "대본 작성 완료")

    def run_gamma(self):
        script = self.txt_result.get("1.0", tk.END).strip()
        if len(script) < 50:
            return

        key = get_api_key(GAMMA_KEY_FILE)
        if not key:
            self.clipboard_clear(); self.clipboard_append(script[:5000])
            webbrowser.open("https://gamma.app/create")
            return

        self.popup = LoadingOverlay(self, "Gamma API 전송 중...")
        threading.Thread(target=self.thread_gamma, args=(script, key, self.popup), daemon=True).start()

    def thread_gamma(self, script, key, loading):
        try:
            self.after(0, lambda: self.open_gamma_web(script))
        except Exception:
            pass
        finally:
            self.after(0, loading.close)

    def open_gamma_web(self, script):
        self.clipboard_clear(); self.clipboard_append(script[:5000])
        webbrowser.open("https://gamma.app/create")
        messagebox.showinfo("Gamma", "대본 복사됨. 붙여넣기 하세요.")

    def send_chat(self):
        msg = self.ent_chat.get(); self.ent_chat.delete(0, tk.END)
        self.txt_chat.configure(state="normal"); self.txt_chat.insert(tk.END, f"나: {msg}\n"); self.txt_chat.configure(state="disabled")
        threading.Thread(target=self.thread_chat, args=(msg,), daemon=True).start()

    def thread_chat(self, msg):
        try:
            model = genai.GenerativeModel('models/gemini-2.0-flash')
            res = model.generate_content(msg).text
            self.after(0, lambda: self.update_chat(res))
        except Exception:
            pass

    def update_chat(self, res):
        self.txt_chat.configure(state="normal"); self.txt_chat.insert(tk.END, f"AI: {res}\n\n"); self.txt_chat.see(tk.END); self.txt_chat.configure(state="disabled")

    def recommend_idea(self):
        script = self.txt_result.get("1.0", tk.END).strip()
        if not script:
            return
        self.popup = LoadingOverlay(self, "아이디어 추출...")
        threading.Thread(target=self.thread_idea, args=(script, self.popup), daemon=True).start()

    def thread_idea(self, script, loading):
        try:
            model = genai.GenerativeModel('models/gemini-2.0-flash')
            res = model.generate_content(f"썸네일 3개 추천:\n{script[:2000]}\nFormat: Main | Sub | Prompt").text
            items = [l.strip() for l in res.split('\n') if "|" in l]
            self.after(0, lambda: self.update_idea_list(items))
        except Exception:
            pass
        finally:
            self.after(0, loading.close)

    def update_idea_list(self, items):
        self.list_ideas.delete(0, tk.END)
        for i in items:
            self.list_ideas.insert(tk.END, i)

    def on_idea_select(self, e):
        if self.list_ideas.curselection():
            p = self.list_ideas.get(self.list_ideas.curselection()[0]).split("|")
            if len(p) >= 3:
                self.ent_main.delete(0, tk.END); self.ent_main.insert(0, p[0].strip())
                self.ent_sub.delete(0, tk.END); self.ent_sub.insert(0, p[1].strip())
                self.txt_scene.delete("1.0", tk.END); self.txt_scene.insert("1.0", p[2].strip())

    def gen_thumb(self):
        m = self.ent_main.get(); s = self.ent_sub.get(); d = self.txt_scene.get("1.0", tk.END)
        k = self.ent_key.get(); st = self.cb_style.get()
        self.popup = LoadingOverlay(self, "렌더링 중...")
        threading.Thread(target=self.thread_thumb, args=(m, s, d, k, st, self.popup), daemon=True).start()

    def thread_thumb(self, m, s, d, k, st, loading):
        try:
            model = genai.GenerativeModel('models/gemini-3-pro-image-preview')
            prompt = f"Thumbnail. Text:'{m}'(BIG), '{s}'. Key:'{k}'. Visual:{d}, {st}. High Quality."
            res = model.generate_content(f"Draw: {prompt}")
            if res.parts:
                img = Image.open(io.BytesIO(res.parts[0].inline_data.data))
                self.current_thumb = img
                fn = f"Thumb_{clean_filename(m)}.png"
                img.save(os.path.join(OUTPUT_DIR, fn))
                self.after(0, self.update_preview)
        except Exception as e:
            self.after(0, lambda: log(self.txt_log, f"썸네일 에러: {e}", "error"))
        finally:
            self.after(0, loading.close)

    def update_preview(self):
        if self.current_thumb:
            w = self.lbl_preview.winfo_width()
            h = int(self.current_thumb.height * (w / self.current_thumb.width))
            tk_img = ImageTk.PhotoImage(self.current_thumb.resize((w, h), Image.Resampling.LANCZOS))
            self.lbl_preview.config(image=tk_img, text=""); self.lbl_preview.image = tk_img

    def resize_preview(self, e):
        self.update_preview()


if __name__ == "__main__":
    app = JeongseokApp()
    app.mainloop()
