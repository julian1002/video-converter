# -*- coding: utf-8 -*-
"""
離線影片轉檔工具 - 圖形介面 (tkinter，Python 內建)
執行：python vconv_gui.py
"""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import vconv_core as core
import vconv_update as upd

FPS_CHOICES = ["23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"]
RES_CHOICES = ["原始解析度", "3840 (4K)", "2560 (1440p)", "1920 (1080p)",
               "1280 (720p)", "960", "640"]
LEVEL_COLORS = {"極高": "#1b7f3b", "高畫": "#2e9e4f", "標準": "#c08a00",
                "低畫": "#d0601a", "極低": "#c0392b"}


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"離線影片轉檔工具 v{core.APP_VERSION}")
        root.geometry("980x720")
        root.minsize(900, 640)
        self._setup_style()

        self.ffmpeg = core.find_tool("ffmpeg")
        self.ffprobe = core.find_tool("ffprobe")
        self.encoders = set()
        self.src = None
        self.sequences = []
        self.seq_folder = ""
        self.proc = None
        self.cancel_flag = False
        self.sample_result = None      # (bytes, vbps) 取樣結果，設定變動時失效
        self.q = queue.Queue()
        self._preview_img = None
        self._busy = False

        self._build_menu()
        self.page1 = ttk.Frame(root, padding=14)
        self.page2 = ttk.Frame(root, padding=14)
        self._build_page1()
        self._build_page2()
        self.show_page(1)
        root.after(100, self._poll_queue)
        root.after(200, self._check_ffmpeg)

    # ------------------------------------------------------------ 樣式
    def _setup_style(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        base = "Microsoft JhengHei UI" if sys.platform == "win32" else (
            "PingFang TC" if sys.platform == "darwin" else "Noto Sans CJK TC")
        self.font = (base, 10)
        self.font_b = (base, 10, "bold")
        self.font_h = (base, 15, "bold")
        self.font_big = (base, 18, "bold")
        self.root.option_add("*Font", self.font)
        style.configure(".", font=self.font)
        style.configure("H.TLabel", font=self.font_h)
        style.configure("B.TLabel", font=self.font_b)
        style.configure("Big.TButton", font=self.font_b, padding=(14, 6))
        style.configure("Note.TLabel", foreground="#666")

    def _build_menu(self):
        m = tk.Menu(self.root)
        s = tk.Menu(m, tearoff=0)
        s.add_command(label="指定 ffmpeg 位置…", command=self._pick_ffmpeg)
        s.add_command(label="顯示 ffmpeg 版本", command=self._show_ffmpeg_ver)
        m.add_cascade(label="設定", menu=s)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="檢查更新…", command=self.open_updater)
        m.add_cascade(label="說明", menu=h)
        self.root.config(menu=m)

    def _check_ffmpeg(self):
        if not self.ffmpeg or not self.ffprobe:
            if messagebox.askyesno(
                    "找不到 FFmpeg",
                    "本工具需要 ffmpeg 與 ffprobe 才能轉檔。\n\n"
                    "要現在自動下載安裝嗎？（需要網路，約 80~150 MB）\n\n"
                    "選「否」可稍後從「設定 → 指定 ffmpeg 位置」手動選擇。"):
                self.open_updater()
            return
        self.encoders = core.list_encoders(self.ffmpeg)
        self.codec_cb["values"] = core.available_codecs(self.encoders)
        self.status1.set(f"FFmpeg：{self.ffmpeg}")

    def _pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="選擇 ffmpeg 執行檔")
        if not p:
            return
        d = os.path.dirname(p)
        exe = ".exe" if sys.platform == "win32" else ""
        probe = os.path.join(d, "ffprobe" + exe)
        if not os.path.isfile(probe):
            messagebox.showerror("錯誤", "同資料夾內找不到 ffprobe，兩者需放在一起。")
            return
        self.ffmpeg, self.ffprobe = p, probe
        self._check_ffmpeg()

    def open_updater(self):
        if getattr(self, "_upd_win", None) and self._upd_win.winfo_exists():
            self._upd_win.lift()
            return
        self._upd_win = UpdateDialog(self).win

    def _show_ffmpeg_ver(self):
        if self.ffmpeg:
            v = core.run([self.ffmpeg, "-version"]).stdout.splitlines()[:1]
            messagebox.showinfo("FFmpeg", (v[0] if v else "?") + "\n" + self.ffmpeg)

    def show_page(self, n):
        (self.page2 if n == 1 else self.page1).pack_forget()
        (self.page1 if n == 1 else self.page2).pack(fill="both", expand=True)

    # ============================================================ 第一階段
    def _build_page1(self):
        p = self.page1
        ttk.Label(p, text="步驟 1　選擇來源", style="H.TLabel").pack(anchor="w")
        ttk.Label(p, text="可選擇含有序列圖（JPG / PNG / EXR / TIFF / DPX…）的資料夾，或單一影片檔"
                          "（MP4 / MOV / AVI / WMV / MKV…）", style="Note.TLabel").pack(anchor="w", pady=(2, 10))

        bar = self.src_bar = ttk.Frame(p)
        bar.pack(fill="x")
        ttk.Button(bar, text="📁 選擇序列圖資料夾", style="Big.TButton",
                   command=self.pick_folder).pack(side="left")
        ttk.Button(bar, text="🎞 選擇影片檔", style="Big.TButton",
                   command=self.pick_video).pack(side="left", padx=10)
        ttk.Button(bar, text="🔄 檢查更新", command=self.open_updater).pack(side="right")

        self.seq_bar = ttk.Frame(p)
        ttk.Label(self.seq_bar, text="序列：").pack(side="left")
        self.seq_var = tk.StringVar()
        self.seq_cb = ttk.Combobox(self.seq_bar, textvariable=self.seq_var, state="readonly", width=48)
        self.seq_cb.pack(side="left")
        self.seq_cb.bind("<<ComboboxSelected>>", lambda e: self._load_seq())
        ttk.Label(self.seq_bar, text="　幀率 (fps)：").pack(side="left")
        self.seq_fps = tk.StringVar(value="24")
        fps_cb = ttk.Combobox(self.seq_bar, textvariable=self.seq_fps, values=FPS_CHOICES, width=8)
        fps_cb.pack(side="left")
        fps_cb.bind("<<ComboboxSelected>>", lambda e: self._load_seq())
        fps_cb.bind("<Return>", lambda e: self._load_seq())
        fps_cb.bind("<FocusOut>", lambda e: self._load_seq())

        body = ttk.Frame(p)
        body.pack(fill="both", expand=True, pady=12)
        self.info = tk.Text(body, height=14, width=50, relief="solid", borderwidth=1,
                            padx=10, pady=8, state="disabled", wrap="word")
        self.info.pack(side="left", fill="both", expand=True)
        self.preview = ttk.Label(body, text="（預覽）", anchor="center", width=40,
                                 relief="solid", borderwidth=1)
        self.preview.pack(side="left", fill="both", padx=(12, 0))

        bot = ttk.Frame(p)
        bot.pack(fill="x")
        self.status1 = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.status1, style="Note.TLabel").pack(side="left")
        self.next_btn = ttk.Button(bot, text="下一步：轉檔設定 ▶", style="Big.TButton",
                                   command=self.goto_page2, state="disabled")
        self.next_btn.pack(side="right")
        self._set_info("尚未選擇來源。")

    def _set_info(self, text):
        self.info.config(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", text)
        self.info.config(state="disabled")

    def _need_ffmpeg(self):
        if not self.ffmpeg or not self.ffprobe:
            self._check_ffmpeg()
            return False
        return True

    def pick_folder(self):
        if not self._need_ffmpeg():
            return
        d = filedialog.askdirectory(title="選擇序列圖所在資料夾")
        if not d:
            return
        seqs = core.scan_sequences(d)
        if not seqs:
            messagebox.showwarning("沒有序列", "此資料夾中找不到帶編號的序列圖檔\n"
                                   "（例如 shot_0001.png、render.001.exr）。")
            return
        self.seq_folder, self.sequences = d, seqs
        labels = [f"{s['prefix']}{'#' * max(s['pad'], 1)}{s['ext']}　（{len(s['numbers'])} 張）"
                  for s in seqs]
        self.seq_cb["values"] = labels
        self.seq_cb.current(0)
        self.seq_bar.pack(fill="x", pady=(10, 0), after=self.src_bar)
        self._load_seq()

    def _load_seq(self):
        if not self.sequences or self.seq_cb.current() < 0:
            return
        try:
            fps = float(self.seq_fps.get())
            if fps <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("錯誤", "幀率需為正數")
            return
        seq = self.sequences[self.seq_cb.current()]
        try:
            self.src = core.load_sequence(self.ffprobe, self.seq_folder, seq, fps)
        except Exception as e:
            messagebox.showerror("讀取失敗", str(e))
            return
        self._after_load()

    def pick_video(self):
        if not self._need_ffmpeg():
            return
        exts = " ".join("*" + e for e in sorted(core.VIDEO_EXTS))
        f = filedialog.askopenfilename(title="選擇影片檔",
                                       filetypes=[("影片檔", exts), ("所有檔案", "*.*")])
        if not f:
            return
        try:
            self.src = core.load_video(self.ffprobe, f)
        except Exception as e:
            messagebox.showerror("讀取失敗", str(e))
            return
        self.seq_bar.pack_forget()
        self.sequences = []
        self._after_load()

    def _after_load(self):
        s = self.src
        text = s.describe()
        if s.kind == "sequence" and s.missing_frames:
            text += f"\n\n※ 序列中間有缺格，將只轉出前段連續的 {s.frames} 張。"
        if not s.width:
            text += "\n\n⚠ 無法讀取圖檔尺寸，ffmpeg 可能不支援此格式。"
        self._set_info(text)
        self.next_btn.config(state="normal" if s.width and s.frames > 0 else "disabled")
        self._make_preview()

    def _make_preview(self):
        s = self.src
        tmp = os.path.join(tempfile.gettempdir(), "vconv_preview.png")
        cmd = [self.ffmpeg, "-hide_banner", "-v", "error", "-y"]
        if s.kind == "sequence":
            if s.ext == ".exr":
                cmd += ["-apply_trc", "iec61966_2_1"]
            cmd += ["-i", s.first_file]
        else:
            cmd += ["-ss", f"{s.duration * 0.1:.2f}", "-i", s.path]
        cmd += ["-frames:v", "1", "-vf", "scale=320:-2", "-update", "1", tmp]

        def work():
            r = core.run(cmd)
            self.q.put(("preview", tmp if r.returncode == 0 and os.path.exists(tmp) else None))
        threading.Thread(target=work, daemon=True).start()

    def goto_page2(self):
        s = self.src
        base, _ = os.path.splitext(s.path)
        if s.kind == "sequence":
            name = os.path.basename(base).replace("%", "").rstrip("0123456789d_.- ") or "output"
            self._out_base = os.path.join(os.path.dirname(s.path), name)
        else:
            self._out_base = base + "_轉檔"
        self.out_var.set("")
        is_vid = s.kind == "video" and s.has_audio
        for w in self.audio_widgets:
            w.config(state="readonly" if is_vid else "disabled")
        self.exr_chk.config(state="normal" if s.ext == ".exr" else "disabled")
        self.src_label.set(f"來源：{os.path.basename(s.path)}　{s.width}×{s.height}　"
                           f"{s.fps:g} fps　{s.frames} 格　{core.fmt_time(s.frames / s.fps)}")
        if not self.codec_var.get():
            vals = list(self.codec_cb["values"])
            if vals:
                self.codec_var.set(vals[0])
        self._on_codec()
        self.show_page(2)

    # ============================================================ 第二階段
    def _build_page2(self):
        p = self.page2
        top = ttk.Frame(p)
        top.pack(fill="x")
        ttk.Label(top, text="步驟 2　轉檔設定", style="H.TLabel").pack(side="left")
        ttk.Button(top, text="◀ 上一步", command=lambda: self.show_page(1)).pack(side="right")
        self.src_label = tk.StringVar()
        ttk.Label(p, textvariable=self.src_label, style="Note.TLabel").pack(anchor="w", pady=(2, 8))

        mid = ttk.Frame(p)
        mid.pack(fill="both", expand=True)
        left = ttk.LabelFrame(mid, text="壓縮設定", padding=10)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.LabelFrame(mid, text="畫質與檔案大小預估", padding=12)
        right.pack(side="left", fill="both", padx=(12, 0))

        g = left
        g.columnconfigure(1, weight=1)
        r = 0

        def row(label):
            nonlocal r
            ttk.Label(g, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            r += 1
            return r - 1

        # 編碼格式
        i = row("壓縮格式")
        self.codec_var = tk.StringVar()
        self.codec_cb = ttk.Combobox(g, textvariable=self.codec_var, state="readonly", width=28)
        self.codec_cb.grid(row=i, column=1, sticky="w")
        self.codec_cb.bind("<<ComboboxSelected>>", lambda e: self._on_codec())
        self.codec_note = tk.StringVar()
        ttk.Label(g, textvariable=self.codec_note, style="Note.TLabel").grid(
            row=r, column=1, sticky="w")
        r += 1

        # 容器
        i = row("輸出檔案類型")
        self.cont_var = tk.StringVar()
        self.cont_cb = ttk.Combobox(g, textvariable=self.cont_var, state="readonly", width=10)
        self.cont_cb.grid(row=i, column=1, sticky="w")
        self.cont_cb.bind("<<ComboboxSelected>>", lambda e: self._on_container())

        # 位元率模式
        i = row("控制方式")
        self.mode_var = tk.StringVar(value="quality")
        mf = ttk.Frame(g)
        mf.grid(row=i, column=1, sticky="w")
        self.mode_q = ttk.Radiobutton(mf, text="畫質優先（固定品質）", value="quality",
                                      variable=self.mode_var, command=self._on_mode)
        self.mode_q.pack(side="left")
        self.mode_b = ttk.Radiobutton(mf, text="指定位元率（大小精準）", value="bitrate",
                                      variable=self.mode_var, command=self._on_mode)
        self.mode_b.pack(side="left", padx=10)

        # 畫質滑桿
        i = row("畫質")
        qf = ttk.Frame(g)
        qf.grid(row=i, column=1, sticky="we")
        ttk.Label(qf, text="低", style="Note.TLabel").pack(side="left")
        self.q_scale = tk.Scale(qf, orient="horizontal", showvalue=False, length=260,
                                command=lambda v: self._on_value(), resolution=1)
        self.q_scale.pack(side="left", fill="x", expand=True)
        ttk.Label(qf, text="高", style="Note.TLabel").pack(side="left")
        self.q_text = tk.StringVar()
        ttk.Label(qf, textvariable=self.q_text, width=18, style="B.TLabel").pack(side="left", padx=8)

        # 位元率
        i = row("位元率 (kbps)")
        self.br_var = tk.StringVar(value="8000")
        self.br_sp = ttk.Spinbox(g, textvariable=self.br_var, from_=100, to=500000,
                                 increment=500, width=10, command=self._changed)
        self.br_sp.grid(row=i, column=1, sticky="w")
        self.br_sp.bind("<KeyRelease>", lambda e: self._changed())

        # 速度
        i = row("編碼速度")
        self.preset_var = tk.StringVar()
        self.preset_cb = ttk.Combobox(g, textvariable=self.preset_var, state="readonly", width=12)
        self.preset_cb.grid(row=i, column=1, sticky="w")
        self.preset_cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
        self.preset_note = tk.StringVar()
        ttk.Label(g, textvariable=self.preset_note, style="Note.TLabel").grid(
            row=i, column=1, sticky="w", padx=(110, 0))

        # 色深
        i = row("色彩深度")
        self.bits_var = tk.StringVar(value="8-bit")
        self.bits_cb = ttk.Combobox(g, textvariable=self.bits_var, state="readonly", width=10)
        self.bits_cb.grid(row=i, column=1, sticky="w")
        self.bits_cb.bind("<<ComboboxSelected>>", lambda e: self._changed())

        # 解析度 / 幀率
        i = row("輸出寬度")
        self.res_var = tk.StringVar(value=RES_CHOICES[0])
        res_cb = ttk.Combobox(g, textvariable=self.res_var, values=RES_CHOICES, state="readonly", width=16)
        res_cb.grid(row=i, column=1, sticky="w")
        res_cb.bind("<<ComboboxSelected>>", lambda e: self._changed())

        i = row("輸出幀率")
        self.ofps_var = tk.StringVar(value="與來源相同")
        ofps = ttk.Combobox(g, textvariable=self.ofps_var, values=["與來源相同"] + FPS_CHOICES,
                            state="readonly", width=12)
        ofps.grid(row=i, column=1, sticky="w")
        ofps.bind("<<ComboboxSelected>>", lambda e: self._changed())

        # 音訊
        i = row("音訊")
        af = ttk.Frame(g)
        af.grid(row=i, column=1, sticky="w")
        self.audio_var = tk.StringVar(value="重新壓縮")
        a1 = ttk.Combobox(af, textvariable=self.audio_var, values=["重新壓縮", "直接複製", "無聲"],
                          state="readonly", width=10)
        a1.pack(side="left")
        a1.bind("<<ComboboxSelected>>", lambda e: self._changed())
        self.akbps_var = tk.StringVar(value="192")
        a2 = ttk.Combobox(af, textvariable=self.akbps_var, values=["96", "128", "192", "256", "320"],
                          state="readonly", width=6)
        a2.pack(side="left", padx=6)
        a2.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Label(af, text="kbps（僅影片來源）", style="Note.TLabel").pack(side="left")
        self.audio_widgets = [a1, a2]

        i = row("EXR 色彩")
        self.exr_var = tk.BooleanVar(value=True)
        self.exr_chk = ttk.Checkbutton(g, text="線性 (Linear) 轉 sRGB（一般 EXR 算圖建議勾選）",
                                       variable=self.exr_var, command=self._changed)
        self.exr_chk.grid(row=i, column=1, sticky="w")

        # 輸出路徑
        i = row("輸出檔案")
        of = ttk.Frame(g)
        of.grid(row=i, column=1, sticky="we")
        self.out_var = tk.StringVar()
        ttk.Entry(of, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(of, text="另存為…", command=self._pick_output).pack(side="left", padx=(6, 0))

        # ---------- 右側預估面板
        self.level_lbl = tk.Label(right, text="—", font=self.font_big, width=16, pady=10,
                                  fg="white", bg="#888")
        self.level_lbl.pack(fill="x")
        self.meter = ttk.Progressbar(right, maximum=100, length=220)
        self.meter.pack(fill="x", pady=(8, 12))
        self.est_size = tk.StringVar(value="—")
        self.est_rate = tk.StringVar(value="—")
        self.est_ratio = tk.StringVar(value="—")
        self.est_method = tk.StringVar(value="")
        for label, var, bold in (("預估檔案大小", self.est_size, True),
                                 ("預估影像位元率", self.est_rate, False),
                                 ("相較來源", self.est_ratio, False)):
            f = ttk.Frame(right)
            f.pack(fill="x", pady=2)
            ttk.Label(f, text=label).pack(side="left")
            ttk.Label(f, textvariable=var, style="B.TLabel" if bold else "TLabel").pack(side="right")
        ttk.Label(right, textvariable=self.est_method, style="Note.TLabel",
                  wraplength=230, justify="left").pack(anchor="w", pady=(8, 6))
        self.sample_btn = ttk.Button(right, text="🔍 實際取樣，精準預估", command=self.run_sample)
        self.sample_btn.pack(fill="x")
        ttk.Label(right, text="（壓縮影片中 3 小段來推算，\n 只需幾秒鐘）", style="Note.TLabel").pack(anchor="w", pady=4)

        # ---------- 底部：執行
        bot = ttk.Frame(p)
        bot.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(bot, text="▶ 開始轉檔", style="Big.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bot, text="■ 取消", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(bot, text="開啟輸出資料夾", command=self._open_folder, state="disabled")
        self.open_btn.pack(side="left")
        self.prog_text = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.prog_text).pack(side="right")
        self.prog = ttk.Progressbar(p, maximum=100)
        self.prog.pack(fill="x", pady=6)
        self.log = tk.Text(p, height=5, relief="solid", borderwidth=1, state="disabled",
                           font=("Consolas", 9) if sys.platform == "win32" else ("Menlo", 9))
        self.log.pack(fill="x")

    # ------------------------------------------------------------ 設定變動
    def _codec(self):
        return core.CODECS.get(self.codec_var.get())

    def _on_codec(self):
        c = self._codec()
        if not c:
            return
        self.codec_note.set(c.get("note", ""))
        self.cont_cb["values"] = c["containers"]
        if self.cont_var.get() not in c["containers"]:
            self.cont_var.set(c["containers"][0])
        if c["mode"] == "profile":
            self.q_scale.config(from_=0, to=len(c["profiles"]) - 1)
            self.mode_var.set("quality")
            self.mode_b.config(state="disabled")
        else:
            lo, hi = c["rng"]
            self.q_scale.config(from_=hi, to=lo)     # 左＝低畫質（數字大）右＝高畫質
            self.mode_b.config(state="normal")
        self.q_scale.set(c["default"])
        self._on_value()
        if c.get("presets"):
            self.preset_cb.config(values=c["presets"], state="readonly")
            self.preset_var.set(c["preset"])
            self.preset_note.set("← 越左越快，越右壓縮效率越好"
                                 if not c["enc"].startswith("libsvt") and c["enc"] != "libvpx-vp9"
                                 else "← 數字越小越慢、壓縮效率越好")
        else:
            self.preset_cb.config(values=[], state="disabled")
            self.preset_var.set("")
            self.preset_note.set("")
        bits = c.get("bits", [8])
        if c["mode"] == "profile":
            self.bits_cb.config(values=["依設定檔"], state="disabled")
            self.bits_var.set("依設定檔")
        else:
            self.bits_cb.config(values=[f"{b}-bit" for b in bits],
                                state="readonly" if len(bits) > 1 else "disabled")
            if self.bits_var.get() not in self.bits_cb["values"]:
                self.bits_var.set(f"{bits[0]}-bit")
        self._on_mode()
        self._on_container()

    def _on_container(self):
        cont = self.cont_var.get()
        if not cont:
            return
        cur = self.out_var.get().strip()
        base = os.path.splitext(cur)[0] if cur else self._out_base
        self.out_var.set(base + "." + cont)
        self._changed()

    def _on_mode(self):
        bitrate = self.mode_var.get() == "bitrate"
        self.q_scale.config(state="disabled" if bitrate else "normal")
        self.br_sp.config(state="normal" if bitrate else "disabled")
        if bitrate:
            # 依目前畫質換算出建議位元率當起始值
            st = self._settings(quiet=True)
            if st:
                st.rate_mode = "quality"
                _, vbps, _ = core.estimate_size(self.src, st)
                self.br_var.set(str(max(100, int(round(vbps / 1000 / 100) * 100))))
        self._changed()

    def _on_value(self):
        c = self._codec()
        if not c:
            return
        v = int(float(self.q_scale.get()))
        if c["mode"] == "profile":
            self.q_text.set(c["profiles"][v][0])
        elif c["mode"] == "q":
            self.q_text.set(f"Q 值 {v}")
        elif c["mode"] == "cq":
            self.q_text.set(f"CQ {v}")
        else:
            self.q_text.set(f"CRF {v}")
        self._changed()

    def _settings(self, quiet=False):
        c = self._codec()
        if not c or not self.src:
            return None
        try:
            br = int(float(self.br_var.get() or 0))
        except ValueError:
            br = 0
        res = self.res_var.get()
        scale_w = 0 if res.startswith("原始") else int(res.split()[0])
        ofps = self.ofps_var.get()
        audio = {"重新壓縮": "encode", "直接複製": "copy", "無聲": "none"}[self.audio_var.get()]
        bits = 10 if self.bits_var.get().startswith("10") else 8
        return core.Settings(
            codec=self.codec_var.get(), container=self.cont_var.get(),
            rate_mode=self.mode_var.get(), value=int(float(self.q_scale.get())),
            bitrate_kbps=max(br, 1), preset=self.preset_var.get(), bits=bits,
            scale_w=scale_w, out_fps=0 if ofps == "與來源相同" else float(ofps),
            audio=audio, audio_kbps=int(self.akbps_var.get()), exr_srgb=self.exr_var.get(),
            output=self.out_var.get().strip())

    def _changed(self):
        self.sample_result = None
        self._update_estimate()

    def _update_estimate(self):
        st = self._settings()
        if not st:
            return
        size, vbps, abps = core.estimate_size(self.src, st)
        method = "依經驗公式粗估，實際大小會因畫面複雜度而有差異（約 ±50%）。"
        if self.sample_result:
            size, vbps = self.sample_result
            method = "✔ 已依實際取樣結果推算，誤差通常在 ±10% 以內。"
        elif st.rate_mode == "bitrate":
            method = "指定位元率模式，大小較可預期（誤差約 ±10%）。"

        # 畫質等級
        c = self._codec()
        w, h = core.out_dims(self.src, st.scale_w)
        fps = st.out_fps or self.src.fps
        if st.rate_mode == "bitrate" and c["mode"] != "profile":
            bpp = st.bitrate_kbps * 1000 / (w * h * fps)
            level = core.level_from_bpp(bpp, st.codec)
            score = {"極高": 92, "高畫": 72, "標準": 52, "低畫": 32, "極低": 10}[level[:2]]
        else:
            level, score = core.quality_level(st.codec, st.value)
        self.level_lbl.config(text=level.split("（")[0], bg=LEVEL_COLORS[level[:2]])
        self.meter["value"] = max(3, score)
        self.est_size.set(core.fmt_size(size))
        self.est_rate.set(core.fmt_rate(vbps) + (f"（＋音訊 {core.fmt_rate(abps)}）" if abps else ""))
        if self.src.size_bytes:
            ratio = size / self.src.size_bytes
            self.est_ratio.set(f"{ratio * 100:.0f}%（{'變小' if ratio < 1 else '變大'}）")
        self.est_method.set(f"{w}×{h}　{fps:g} fps\n" + method)

    def _pick_output(self):
        cont = self.cont_var.get() or "mp4"
        cur = self.out_var.get()
        f = filedialog.asksaveasfilename(
            title="輸出檔案", defaultextension="." + cont,
            initialdir=os.path.dirname(cur) if cur else None,
            initialfile=os.path.basename(cur) if cur else None,
            filetypes=[(cont.upper(), "*." + cont)])
        if f:
            if not f.lower().endswith("." + cont):
                f = os.path.splitext(f)[0] + "." + cont
            self.out_var.set(f)

    # ------------------------------------------------------------ 取樣預估
    def run_sample(self):
        st = self._settings()
        if not st or self._busy:
            return
        self._set_busy(True, sampling=True)
        self.est_method.set("取樣壓縮中，請稍候…")
        src = self.src

        def work():
            try:
                res = core.sample_estimate(self.ffmpeg, src, st, cancel=lambda: self.cancel_flag)
                self.q.put(("sample", res))
            except Exception as e:
                self.q.put(("sample_err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 轉檔
    def start(self):
        st = self._settings()
        if not st or self._busy:
            return
        if not st.output:
            messagebox.showerror("錯誤", "請指定輸出檔案")
            return
        out = os.path.abspath(st.output)
        st.output = out
        if self.src.kind == "video" and os.path.abspath(self.src.path) == out:
            messagebox.showerror("錯誤", "輸出檔不能與來源檔相同")
            return
        if not os.path.isdir(os.path.dirname(out)):
            messagebox.showerror("錯誤", "輸出資料夾不存在")
            return
        if os.path.exists(out) and not messagebox.askyesno("檔案已存在", f"要覆蓋現有檔案嗎？\n{out}"):
            return
        cmd = core.build_command(self.ffmpeg, self.src, st)
        cmd[1:1] = ["-nostats", "-progress", "pipe:1"]
        self._log_clear()
        self._log(" ".join(f'"{a}"' if " " in a else a for a in cmd))
        self._set_busy(True)
        self.open_btn.config(state="disabled")
        self.prog["value"] = 0
        total = self.src.frames * ((st.out_fps or self.src.fps) / self.src.fps)
        self._job_out = out
        threading.Thread(target=self._worker, args=(cmd, total), daemon=True).start()

    def _worker(self, cmd, total_frames):
        t0 = time.time()
        err_tail = []
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", creationflags=core._NO_WINDOW)
        except OSError as e:
            self.q.put(("done", (False, str(e))))
            return

        def read_err():
            for line in self.proc.stderr:
                err_tail.append(line.rstrip())
                del err_tail[:-30]
        te = threading.Thread(target=read_err, daemon=True)
        te.start()
        state = {}
        for line in self.proc.stdout:
            if core.parse_progress(line, state):
                frame = int(state.get("frame", "0") or 0)
                pct = min(100.0, 100.0 * frame / total_frames) if total_frames else 0
                el = time.time() - t0
                eta = el / pct * (100 - pct) if pct > 0.5 else None
                self.q.put(("progress", (pct, frame, state.get("fps", "0"), el, eta,
                                         int(state.get("total_size", "0") or 0))))
        self.proc.wait()
        te.join(timeout=2)
        rc = self.proc.returncode
        self.proc = None
        if self.cancel_flag:
            self.q.put(("done", (False, "已取消")))
        elif rc == 0:
            self.q.put(("done", (True, time.time() - t0)))
        else:
            self.q.put(("done", (False, "\n".join(err_tail[-12:]))))

    def cancel(self):
        self.cancel_flag = True
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def _set_busy(self, busy, sampling=False):
        self._busy = busy
        if busy:
            self.cancel_flag = False
        st = "disabled" if busy else "normal"
        self.start_btn.config(state=st)
        self.sample_btn.config(state=st)
        self.cancel_btn.config(state="normal" if busy and not sampling else "disabled")

    # ------------------------------------------------------------ 主執行緒更新
    def _poll_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "_q_" + kind)(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _q_preview(self, path):
        if not path:
            self.preview.config(image="", text="（無法產生預覽）")
            return
        try:
            self._preview_img = tk.PhotoImage(file=path)
            self.preview.config(image=self._preview_img, text="")
        except tk.TclError:
            self.preview.config(image="", text="（無法顯示預覽）")

    def _q_sample(self, res):
        self._set_busy(False)
        if res:
            self.sample_result = res
        self._update_estimate()

    def _q_sample_err(self, msg):
        self._set_busy(False)
        self._update_estimate()
        self._log(msg)
        messagebox.showerror("取樣失敗", msg[-600:])

    def _q_progress(self, d):
        pct, frame, fps, el, eta, size = d
        self.prog["value"] = pct
        eta_s = core.fmt_time(eta).split(".")[0] if eta is not None else "--:--:--"
        self.prog_text.set(f"{pct:5.1f}%　第 {frame} 格　{fps} fps　"
                           f"已用 {core.fmt_time(el).split('.')[0]}　剩餘 {eta_s}　"
                           f"目前 {core.fmt_size(size)}")

    def _q_done(self, d):
        ok, info = d
        self._set_busy(False)
        if ok:
            self.prog["value"] = 100
            size = os.path.getsize(self._job_out) if os.path.exists(self._job_out) else 0
            msg = f"轉檔完成！耗時 {core.fmt_time(info).split('.')[0]}，檔案大小 {core.fmt_size(size)}"
            self.prog_text.set(msg)
            self._log(msg)
            self.open_btn.config(state="normal")
            messagebox.showinfo("完成", msg + f"\n\n{self._job_out}")
        else:
            self.prog_text.set("轉檔失敗" if info != "已取消" else "已取消")
            self._log(info)
            if info != "已取消":
                messagebox.showerror("轉檔失敗", info[-800:])
            elif os.path.exists(self._job_out):
                try:
                    os.remove(self._job_out)   # 刪除未完成的檔案
                except OSError:
                    pass

    def _open_folder(self):
        folder = os.path.dirname(self._job_out)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(self._job_out)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", self._job_out])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")


class UpdateDialog:
    """檢查更新視窗：比對目前版本與可更新版本，顯示下載網址與進度"""

    def __init__(self, app):
        self.app = app
        self.cfg = upd.load_config()
        self.q = queue.Queue()
        self.remote = {}          # "ffmpeg" / "app" → 遠端資訊
        self.local_ff = {}
        self.cancel_flag = False
        self.downloading = False
        w = self.win = tk.Toplevel(app.root)
        w.title("檢查更新")
        w.geometry("760x560")
        w.transient(app.root)
        w.protocol("WM_DELETE_WINDOW", self.close)
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="版本與更新", style="H.TLabel").pack(side="left")
        ttk.Button(top, text="🔄 重新檢查", command=self.check).pack(side="right")
        src = ttk.Frame(f)
        src.pack(fill="x", pady=(8, 4))
        ttk.Label(src, text="FFmpeg 下載來源：").pack(side="left")
        self.src_var = tk.StringVar(value=upd.SOURCES.get(self.cfg.get("ffmpeg_source"), upd.SOURCES["btbn"]))
        cb = ttk.Combobox(src, textvariable=self.src_var, values=list(upd.SOURCES.values()),
                          state="readonly", width=34)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_source())

        self.tree = ttk.Treeview(f, columns=("cur", "new", "status"), height=3, selectmode="browse")
        for col, text, wd in (("#0", "元件", 130), ("cur", "目前版本", 210),
                              ("new", "可更新版本", 210), ("status", "狀態", 170)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=wd, anchor="w")
        self.tree.pack(fill="x", pady=6)
        for iid, name in (("app", "影片轉檔工具"), ("ffmpeg", "FFmpeg"), ("python", "Python")):
            self.tree.insert("", "end", iid=iid, text=name, values=("", "", ""))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_url())

        uf = ttk.Frame(f)
        uf.pack(fill="x", pady=(6, 0))
        ttk.Label(uf, text="下載網址：").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(uf, textvariable=self.url_var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(uf, text="複製", command=self._copy_url).pack(side="left", padx=(6, 0))
        self.size_var = tk.StringVar()
        ttk.Label(f, textvariable=self.size_var, style="Note.TLabel").pack(anchor="w", pady=2)

        self.prog = ttk.Progressbar(f, maximum=100)
        self.prog.pack(fill="x", pady=(8, 2))
        self.prog_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.prog_var).pack(anchor="w")

        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=8)
        self.ff_btn = ttk.Button(bf, text="⬇ 更新 FFmpeg", style="Big.TButton",
                                 command=self.update_ffmpeg, state="disabled")
        self.ff_btn.pack(side="left")
        self.app_btn = ttk.Button(bf, text="⬇ 更新本工具", style="Big.TButton",
                                  command=self.update_app, state="disabled")
        self.app_btn.pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(bf, text="■ 取消下載", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Button(bf, text="關閉", command=self.close).pack(side="right")

        self.log = tk.Text(f, height=8, relief="solid", borderwidth=1, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        w.after(100, self._poll)
        self.check()

    # ---------------------------------------------------- 檢查
    def _source_key(self):
        return next((k for k, v in upd.SOURCES.items() if v == self.src_var.get()), "btbn")

    def _on_source(self):
        self.cfg["ffmpeg_source"] = self._source_key()
        try:
            upd.save_config(self.cfg)
        except OSError:
            pass
        self.check()

    def check(self):
        if self.downloading:
            return
        self._log("正在檢查更新…")
        for iid in ("app", "ffmpeg"):
            self.tree.set(iid, "new", "檢查中…")
            self.tree.set(iid, "status", "")
        self.ff_btn.config(state="disabled")
        self.app_btn.config(state="disabled")
        source, cfg, ff = self._source_key(), dict(self.cfg), self.app.ffmpeg

        def work():
            local = upd.local_ffmpeg(ff)
            self.q.put(("local_ff", local))
            try:
                r = upd.remote_ffmpeg(cfg, source)
                self.q.put(("remote_ff", (r, upd.compare_ffmpeg(local, r))))
            except Exception as e:
                self.q.put(("err", ("ffmpeg", e)))
            try:
                self.q.put(("remote_app", upd.remote_app(cfg)))
            except Exception as e:
                self.q.put(("err", ("app", e)))
        threading.Thread(target=work, daemon=True).start()
        self.tree.set("app", "cur", core.APP_VERSION)
        self.tree.set("python", "cur", sys.version.split()[0])
        self.tree.set("python", "new", "—")
        self.tree.set("python", "status", "由安裝程式管理")

    def _q_local_ff(self, local):
        self.local_ff = local
        self.tree.set("ffmpeg", "cur", local["raw"])

    def _q_remote_ff(self, data):
        r, (need, status) = data
        self.remote["ffmpeg"] = r
        self.tree.set("ffmpeg", "new", r["label"])
        self.tree.set("ffmpeg", "status", ("⬆ " if need else "✔ ") + status)
        self.ff_btn.config(state="normal", text="⬇ 更新 FFmpeg" if need else "⬇ 重新安裝 FFmpeg")
        self._log(f"FFmpeg 最新：{r['label']}　→ {status}")
        self.tree.selection_set("ffmpeg")
        self._show_url()

    def _q_remote_app(self, r):
        if r is None:
            self.tree.set("app", "new", "—")
            self.tree.set("app", "status", "未設定更新來源")
            self._log("本工具更新來源未設定：可在 config.json 的 app_manifest_url 填入更新清單網址。")
            return
        self.remote["app"] = r
        cur = upd.parse_version(core.APP_VERSION)
        need = bool(r["ver"] and r["ver"] > cur)
        self.tree.set("app", "new", r["label"])
        self.tree.set("app", "status", "⬆ 有新版本" if need else "✔ 已是最新")
        if need:
            self.app_btn.config(state="normal")
            if r.get("notes"):
                self._log("更新內容：" + r["notes"])

    def _q_err(self, data):
        which, e = data
        msg = str(e)
        if "urlopen" in msg or "getaddrinfo" in msg or "timed out" in msg:
            msg += "（請確認網路連線或 Proxy 設定）"
        self.tree.set(which, "new", "無法取得")
        self.tree.set(which, "status", "✖ 連線失敗")
        self._log(f"[{which}] 檢查失敗：{msg}")

    def _show_url(self):
        sel = self.tree.selection()
        r = self.remote.get(sel[0]) if sel else None
        if not r:
            self.url_var.set("")
            self.size_var.set("")
            return
        if sel[0] == "app":
            self.url_var.set(r["url"])
            self.size_var.set(f"更新清單，共 {len(r['files'])} 個檔案")
        else:
            self.url_var.set(r["url"])
            self.size_var.set(f"檔案：{r['name']}　大小：{core.fmt_size(r['size']) if r['size'] else '未知'}")

    def _copy_url(self):
        self.win.clipboard_clear()
        self.win.clipboard_append(self.url_var.get())
        self._log("已複製下載網址")

    # ---------------------------------------------------- 下載 / 安裝
    def _start(self):
        if self.app._busy:
            messagebox.showwarning("請稍候", "目前正在轉檔或取樣，完成後再更新。", parent=self.win)
            return False
        self.downloading = True
        self.cancel_flag = False
        self.ff_btn.config(state="disabled")
        self.app_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.prog["value"] = 0
        return True

    def _progress(self, done, total, speed, unit="bytes"):
        self.q.put(("progress", (done, total, speed, unit)))

    def update_ffmpeg(self):
        r = self.remote.get("ffmpeg")
        if not r or not self._start():
            return
        self.tree.selection_set("ffmpeg")
        self._log(f"開始下載：{r['url']}")
        dest = os.path.join(upd.downloads_dir(), r["name"])

        def work():
            try:
                ok = upd.download(r["url"], dest, self._progress, lambda: self.cancel_flag)
                if not ok:
                    self.q.put(("finished", ("ffmpeg", False, "已取消下載")))
                    return
                self.q.put(("status", "下載完成，解壓縮並安裝中…"))
                path = upd.install_ffmpeg_zip(dest, r, log=lambda m: self.q.put(("log", m)))
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.q.put(("finished", ("ffmpeg", True, path)))
            except Exception as e:
                self.q.put(("finished", ("ffmpeg", False, str(e))))
        threading.Thread(target=work, daemon=True).start()

    def update_app(self):
        r = self.remote.get("app")
        if not r or not self._start():
            return

        def work():
            try:
                ok = upd.install_app_update(
                    r, progress=lambda d, t, s: self._progress(d, t, s, unit="files"),
                    cancel=lambda: self.cancel_flag, log=lambda m: self.q.put(("log", m)))
                self.q.put(("finished", ("app", ok, "" if ok else "已取消")))
            except Exception as e:
                self.q.put(("finished", ("app", False, str(e))))
        threading.Thread(target=work, daemon=True).start()

    def _q_progress(self, d):
        done, total, speed, unit = d
        if unit == "files":
            self.prog["value"] = 100 * done / max(total, 1)
            self.prog_var.set(f"檔案 {min(int(done) + 1, total)} / {total}　{core.fmt_size(speed)}/s")
            return
        if total:
            pct = 100 * done / total
            eta = (total - done) / speed if speed else 0
            self.prog["value"] = pct
            self.prog_var.set(f"{pct:5.1f}%　{core.fmt_size(done)} / {core.fmt_size(total)}　"
                              f"{core.fmt_size(speed)}/s　剩餘 {core.fmt_time(eta).split('.')[0]}")
        else:
            self.prog.config(mode="indeterminate")
            self.prog_var.set(f"{core.fmt_size(done)}　{core.fmt_size(speed)}/s")

    def _q_status(self, text):
        self.prog_var.set(text)
        self._log(text)

    def _q_log(self, text):
        self._log(text)

    def _q_finished(self, d):
        which, ok, info = d
        self.downloading = False
        self.cancel_btn.config(state="disabled")
        self.prog.config(mode="determinate")
        if which == "ffmpeg":
            if ok:
                self.prog["value"] = 100
                self.app.ffmpeg = info
                self.app.ffprobe = os.path.join(os.path.dirname(info), "ffprobe" + core.EXE)
                self.app._check_ffmpeg()
                self.prog_var.set("✔ FFmpeg 安裝完成")
                self._log(f"FFmpeg 已安裝到 {info}")
                messagebox.showinfo("完成", "FFmpeg 已更新完成，可立即使用。", parent=self.win)
                self.check()
            else:
                self.prog_var.set("✖ " + info)
                self._log("FFmpeg 更新失敗：" + info)
                self.ff_btn.config(state="normal")
        else:
            if ok:
                self.prog["value"] = 100
                self.prog_var.set("✔ 程式更新完成")
                if messagebox.askyesno("完成", "程式已更新，要立即重新啟動嗎？", parent=self.win):
                    gui = os.path.join(core._app_dir(), "vconv_gui.py")
                    subprocess.Popen([sys.executable, gui], cwd=core._app_dir(),
                                     creationflags=core._NO_WINDOW)
                    self.app.root.destroy()
            else:
                self.prog_var.set("✖ " + info)
                self._log("程式更新失敗：" + info)
                self.app_btn.config(state="normal")

    def _cancel(self):
        self.cancel_flag = True
        self._log("取消中…")

    def close(self):
        if self.downloading:
            if not messagebox.askyesno("下載中", "正在下載，確定要取消並關閉？", parent=self.win):
                return
            self.cancel_flag = True
        self.win.destroy()

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "_q_" + kind)(data)
        except queue.Empty:
            pass
        try:
            if self.win.winfo_exists():
                self.win.after(100, self._poll)
        except tk.TclError:
            pass

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 高解析度螢幕不模糊
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
