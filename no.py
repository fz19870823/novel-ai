"""
小说自动生成器 - GUI版本 + 用户确认机制
支持：每层生成后用户确认/修改，5秒倒计时自动确认
适配：Grok 180次/日配额
"""

import json
import time
import re
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from functools import lru_cache

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from openai import OpenAI

# ========== 常量定义 ==========
CONFIG_FILE = "novel_generator_config.json"
STATE_FILE = "novel_resume_state.json"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_WORDS_PER_CHAPTER = 1200
DEFAULT_WORDS_PER_SCENE = 280
DEFAULT_TARGET_WORDS = 50000
OUTLINE_BATCH = 15
CONFIRM_COUNTDOWN = 5
WRITE_ROUNDS = 3
TARGET_OUTPUT_WORDS = 8000

def load_config() -> Dict:
    """加载保存的配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARN] 加载配置失败: {e}")
    return {"api_key": "", "base_url": "https://api.x.ai/v1", "model": "grok-4-1-fast-non-reasoning"}

def save_config(config: Dict):
    """保存配置"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ========== 断点续传状态管理 ==========
STATE_FILE = "novel_resume_state.json"

def save_resume_state(state: Dict):
    """保存断点状态到临时文件"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 保存断点状态失败: {e}")

def load_resume_state() -> Dict:
    """加载断点状态，不存在返回 None"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARN] 加载断点状态失败: {e}")
    return None

def clear_resume_state():
    """清除断点文件（生成完成后调用）"""
    if os.path.exists(STATE_FILE):
        try:
            os.remove(STATE_FILE)
        except OSError as e:
            print(f"[WARN] 删除断点文件失败: {e}")

# ========== 确认对话框 ==========
class ConfirmDialog:
    """带倒计时的确认对话框，用户可修改内容"""
    
    def __init__(self, parent, title: str, content: str, prompt: str = ""):
        self.parent = parent
        self.title = title
        self.original_content = content
        self.modified_content = content
        self.prompt = prompt
        self.result = None  # "confirm" 或 "cancel" 或修改后的内容
        self.countdown = 5
        self.countdown_active = True
        self.user_interacted = False  # 用户是否有任何交互（点击/输入）
        
        self._create_dialog()
        self._start_countdown()
    
    def _create_dialog(self):
        """创建对话框"""
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title(self.title)
        self.dialog.geometry("800x600")
        self.dialog.transient(self.parent)
        # 确保主窗口和对话框都可见
        try:
            self.parent.deiconify()
            self.parent.lift()
        except tk.TclError:
            pass
        self.dialog.grab_set()
        self.dialog.lift()
        self.dialog.focus_force()
        
        # 提示信息
        if self.prompt:
            ttk.Label(self.dialog, text=self.prompt, foreground="blue").pack(pady=(10, 5))
        
        # 倒计时标签
        self.countdown_label = ttk.Label(self.dialog, text=f"⏰ {self.countdown} 秒后自动确认...", 
                                          foreground="gray")
        self.countdown_label.pack(pady=(0, 5))
        
        # 可编辑的文本框
        text_frame = ttk.Frame(self.dialog)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.text_widget = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, font=("Courier", 10))
        self.text_widget.pack(fill=tk.BOTH, expand=True)
        self.text_widget.insert(tk.END, self.original_content)
        
        # 绑定用户交互事件（任何键盘或鼠标操作都会取消倒计时）
        self.text_widget.bind("<Key>", self._on_user_interact)
        self.text_widget.bind("<Button-1>", self._on_user_interact)
        self.text_widget.bind("<MouseWheel>", self._on_user_interact)
        
        # 按钮框架
        button_frame = ttk.Frame(self.dialog)
        button_frame.pack(pady=10)
        
        self.confirm_btn = ttk.Button(button_frame, text="确认使用", command=self._on_confirm)
        self.confirm_btn.pack(side=tk.LEFT, padx=5)
        
        self.regenerate_btn = ttk.Button(button_frame, text="重新生成", command=self._on_regenerate)
        self.regenerate_btn.pack(side=tk.LEFT, padx=5)
        
        self.cancel_btn = ttk.Button(button_frame, text="取消（停止生成）", command=self._on_cancel)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)
        
        # 绑定窗口关闭事件
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)
    
    def _on_user_interact(self, event=None):
        """用户交互时取消倒计时"""
        if not self.user_interacted:
            self.user_interacted = True
            self.countdown_active = False
            self.countdown_label.config(text="✏️ 用户编辑模式，请手动确认", foreground="orange")
    
    def _start_countdown(self):
        """启动倒计时"""
        def countdown_loop():
            while self.countdown > 0 and self.countdown_active and not self.user_interacted:
                time.sleep(1)
                self.countdown -= 1
                self.dialog.after(0, self._update_countdown_display)

            # 倒计时结束且无用户交互，自动确认
            if self.countdown <= 0 and self.countdown_active and not self.user_interacted:
                self.dialog.after(0, self._on_confirm)

        threading.Thread(target=countdown_loop, daemon=True).start()
    
    def _update_countdown_display(self):
        """更新倒计时显示"""
        if self.countdown_active and not self.user_interacted:
            self.countdown_label.config(text=f"⏰ {self.countdown} 秒后自动确认...")
    
    def _on_confirm(self):
        """确认使用当前内容"""
        self.modified_content = self.text_widget.get("1.0", tk.END).rstrip("\n")
        self.result = self.modified_content
        self.dialog.grab_release()
        self.dialog.destroy()
    
    def _on_regenerate(self):
        """重新生成 - 返回特殊标记"""
        self.result = "__REGENERATE__"
        self.dialog.grab_release()
        self.dialog.destroy()
    
    def _on_cancel(self):
        """取消生成"""
        self.result = "__CANCEL__"
        self.dialog.grab_release()
        self.dialog.destroy()
    
    def get_result(self):
        """等待对话框关闭并返回结果"""
        self.dialog.wait_window()
        return self.result


# ========== 小说生成器核心 ==========
class NovelGenerator:
    def __init__(self, theme: str, requirements: str, api_key: str, base_url: str, model: str,
                 progress_callback=None, log_callback=None, confirm_callback=None,
                 parent_window=None, resume_state: Dict = None, state_callback=None,
                 chapters_count: int = None, words_per_chapter: int = None):
        self.theme = theme
        self.requirements = requirements
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.confirm_callback = confirm_callback
        self.parent_window = parent_window
        self.state_callback = state_callback
        
        # 解析目标字数
        self.target_words = self._parse_target_words(requirements)
        self.words_per_chapter = words_per_chapter or 1200
        self.words_per_scene = 280
        self.chapters_count = chapters_count or max(5, self.target_words // self.words_per_chapter)
        self.scenes_per_chapter = max(3, self.words_per_chapter // self.words_per_scene)
        
        # 动态计算每批章数（控制单次输出在 8000 字左右，模型能稳定产出）
        target_output = 8000
        self.chunk_size = max(1, min(10, target_output // max(1, self.words_per_chapter)))
        
        self.client = None
        self.call_count = 0
        self.is_running = True
        self._backend_down = False  # 后端不可用标记
        
        # 存储产出
        self.setting_bible = ""
        self.chapter_outlines: List[str] = []
        self.scenes: List[Dict] = []
        self.chapters: Dict[int, List[str]] = {}
        
        # 从断点恢复
        self._resume_chapter = 1
        if resume_state:
            self.setting_bible = resume_state.get("setting_bible", "")
            self.chapter_outlines = resume_state.get("chapter_outlines", [])
            self.scenes = resume_state.get("scenes", [])
            # JSON key 都是 string，转回 int
            saved_chapters = resume_state.get("chapters", {})
            self.chapters = {int(k): v for k, v in saved_chapters.items()}
            self.call_count = resume_state.get("call_count", 0)
            self._resume_chapter = resume_state.get("layer4_batch", 0) + 1  # 下一章
            # 恢复参数（避免重新计算导致不一致）
            if resume_state.get("chapters_count"):
                self.chapters_count = resume_state["chapters_count"]
                self.scenes_per_chapter = resume_state.get("scenes_per_chapter", 4)
                self.words_per_chapter = resume_state.get("words_per_chapter", 1200)
                self.words_per_scene = resume_state.get("words_per_scene", 280)
                self.target_words = resume_state.get("target_words", self.target_words)
            self._log(f"📂 从断点恢复 (已写{sum(len(v) for v in self.chapters.values())}个场景, {self.call_count}次调用)")
        
        self._init_client()
    
    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
    
    def _update_progress(self, value: int, text: str):
        if self.progress_callback:
            self.progress_callback(value, text)
    
    def stop(self):
        self.is_running = False
        self._log("⚠️ 正在停止...")
    
    def _parse_target_words(self, requirements: str) -> int:
        patterns = [
            r'(\d+(?:\.\d+)?)\s*万字',
            r'(\d+)\s*千字',
            r'(\d+)\s*字',
            r'字数[：:]\s*(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, requirements)
            if match:
                num = float(match.group(1))
                if '万' in pattern:
                    return int(num * 10000)
                elif '千' in pattern:
                    return int(num * 1000)
                else:
                    return int(num)
        return 50000
    
    def _init_client(self):
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                             default_headers={"User-Agent": "Mozilla/5.0"})
    
    def _save_state(self, stage: str, layer4_batch: int = 0):
        """保存当前生成进度到断点文件"""
        if not self.state_callback:
            return
        try:
            # chapters 的 key 转 string（JSON 不支持 int key）
            chapters_str = {str(k): v for k, v in self.chapters.items()}
            state = {
                "theme": self.theme,
                "requirements": self.requirements,
                "config": {
                    "api_key": self.api_key,
                    "base_url": self.base_url,
                    "model": self.model
                },
                "stage": stage,
                "layer4_batch": layer4_batch,
                "setting_bible": self.setting_bible,
                "chapter_outlines": self.chapter_outlines,
                "scenes": self.scenes,
                "chapters": chapters_str,
                "call_count": self.call_count,
                "target_words": self.target_words,
                "chapters_count": self.chapters_count,
                "scenes_per_chapter": self.scenes_per_chapter,
                "words_per_chapter": self.words_per_chapter,
                "words_per_scene": self.words_per_scene
            }
            self.state_callback(state)
        except Exception as e:
            self._log(f"⚠️ 保存断点失败: {e}")
    
    def call_grok(self, prompt: str, system: str = "", max_tokens: int = 4000,
                  temperature: float = 0.75) -> str:
        if not self.is_running:
            raise Exception("用户停止生成")

        # 将system信息合并到prompt中（Grok API不支持system参数）
        if system:
            prompt = f"{system}\n\n{prompt}"

        messages = [{"role": "user", "content": prompt}]

        for attempt in range(3):
            if not self.is_running:
                raise Exception("用户停止生成")
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=90
                )
                self.call_count += 1
                content = response.choices[0].message.content
                if not content:
                    self._log(f"⚠️ API返回空内容 ({attempt+1}/3)")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("API连续3次返回空内容")
                self._log(f"📊 累计调用: {self.call_count} 次")
                return content
            except Exception as e:
                if not self.is_running:
                    raise Exception("用户停止生成")
                err_str = str(e)
                self._log(f"⚠️ 调用失败 ({attempt+1}/3): {err_str[:200]}")
                
                # 后端下线（恢复可能长达48小时）— 立即停止，不浪费重试
                if "No healthy provider" in err_str or "no healthy provider" in err_str.lower():
                    raise Exception(f"后端服务不可用（恢复可能需数小时），已自动停止: {err_str[:150]}")
                
                if attempt < 2:
                    # 429 限流等更久，普通错误按指数退避
                    if "429" in err_str or "rate_limit" in err_str.lower():
                        wait_sec = 5 * (3 ** attempt)  # 5s, 15s
                        self._log(f"⏳ 限流等待 {wait_sec} 秒...")
                    else:
                        wait_sec = 2 ** attempt
                    time.sleep(wait_sec)
                else:
                    raise
        return ""
    
    def _confirm_with_user(self, title: str, content: str, prompt: str = "") -> str:
        """
        弹出确认窗口，让用户确认或修改内容
        返回值:
            - 修改后的内容（字符串）
            - "__REGENERATE__" 表示需要重新生成
            - "__CANCEL__" 表示取消整个生成
        """
        if not self.parent_window or not self.confirm_callback:
            return content
        
        # 空内容保护：不弹空对话框，自动重试
        if not content or not content.strip():
            self._log("⚠️ 生成内容为空，自动重试...")
            return "__REGENERATE__"
        
        # 调用确认回调，这里使用同步方式
        result = self.confirm_callback(title, content, prompt)
        
        if result == "__CANCEL__":
            self.is_running = False
            raise Exception("用户取消生成")
        elif result == "__REGENERATE__":
            return "__REGENERATE__"
        else:
            return result
    
    def layer1_setting_bible(self) -> str:
        """第1层：生成设定圣经，需用户确认"""
        self._log("\n🏗️ 第1层：生成设定圣经...")
        self._update_progress(5, "正在生成设定圣经...")
        
        prompt = f"""
        你是一位专业小说架构师。请根据以下用户输入，生成完整的创作圣经。

        【用户提供的主题】
        {self.theme}

        【用户的具体要求】
        {self.requirements}

        目标总字数：{self.target_words}字（约{self.chapters_count}章）

        请一次性输出以下内容：

        ## 一、核心创意解读
        ## 二、世界观设定
        ## 三、角色档案（主角+3-5个配角+反派）
        ## 四、完整情节大纲（三幕结构）
        ## 五、风格锁定（人称、视角、文风、对话风格）

        注意：这部分是后续所有写作的唯一参考标准，请尽量详细。
        """
        
        max_attempts = 3
        for attempt in range(max_attempts):
            result = self.call_grok(prompt, system="你是专业小说架构师，擅长根据用户需求生成详细设定。", max_tokens=12000)
            
            # 用户确认
            confirmed = self._confirm_with_user(
                "确认设定圣经",
                result,
                "请确认或修改以上设定。这是后续所有写作的唯一参考标准。\n5秒后自动确认。"
            )
            
            if confirmed == "__REGENERATE__":
                self._log(f"🔄 用户要求重新生成设定（第{attempt+2}次尝试）")
                continue
            else:
                self.setting_bible = confirmed
                self._save_state("layer2")
                self._log("✅ 设定圣经已确认")
                return confirmed
        
        raise Exception("设定圣经生成失败，已达到最大重试次数")
    
    def layer2_chapter_outlines(self) -> List[str]:
        """第2层：生成章节大纲（大批量时分批确保连贯性），需用户确认"""
        OUTLINE_BATCH = 15
        
        if self.chapters_count <= OUTLINE_BATCH:
            self._log(f"\n📋 第2层：生成{self.chapters_count}章大纲...")
            self._update_progress(15, f"正在生成{self.chapters_count}章大纲...")
            return self._confirm_outlines(self._generate_outlines_batch(1, self.chapters_count, ""))
        else:
            self._log(f"\n📋 第2层：分批生成{self.chapters_count}章大纲（每批{OUTLINE_BATCH}章）...")
            self._update_progress(15, f"正在分批生成{self.chapters_count}章大纲...")
            
            all_outlines = []
            for batch_start in range(1, self.chapters_count + 1, OUTLINE_BATCH):
                if not self.is_running:
                    raise Exception("用户停止生成")
                batch_end = min(batch_start + OUTLINE_BATCH - 1, self.chapters_count)
                prev = "\n\n".join(all_outlines[-3:]) if all_outlines else ""
                batch_outlines = self._generate_outlines_batch(batch_start, batch_end, prev)
                all_outlines.extend(batch_outlines)
                self._log(f"📝 第{batch_start}-{batch_end}章大纲完成")
            
            return self._confirm_outlines(all_outlines)
    
    def _confirm_outlines(self, outlines: List[str]) -> List[str]:
        """确认大纲（用户检查）"""
        formatted = "\n\n".join([f"### {ch}" for ch in outlines])
        confirmed = self._confirm_with_user(
            "确认章节大纲",
            formatted,
            f"请确认或修改以上{len(outlines)}章大纲。\n5秒后自动确认。"
        )
        if confirmed == "__REGENERATE__":
            self._log("🔄 用户要求重新生成大纲")
            return self.layer2_chapter_outlines()
        
        confirmed_chapters = re.split(r'\n###\s*', confirmed)
        confirmed_chapters = [ch.strip() for ch in confirmed_chapters if ch.strip()]
        self.chapter_outlines = confirmed_chapters[:self.chapters_count]
        self._save_state("layer3")
        self._log(f"✅ 章节大纲已确认，共{len(self.chapter_outlines)}章")
        return self.chapter_outlines
    
    def _generate_outlines_batch(self, ch_start: int, ch_end: int, prev_context: str) -> List[str]:
        """生成一批章节大纲（ch_start 到 ch_end），带前文上下文"""
        batch_count = ch_end - ch_start + 1
        prompt = f"""
基于以下小说设定，生成第{ch_start}章到第{ch_end}章的详细大纲（共{batch_count}章）。

【小说设定（完整）】
{self.setting_bible}

【前几章大纲参考】
{prev_context or '（首批，无前文）'}

请逐章输出，每章格式如下：

### 第X章《章标题》
- 核心事件：（50字内）
- 冲突点：
- 情绪基调：
- 角色出场：
- 结尾钩子：

从第{ch_start}章开始，到第{ch_end}章结束。
"""
        max_attempts = 3
        for attempt in range(max_attempts):
            response = self.call_grok(
                prompt,
                system="输出结构化的章节大纲，注意前后连贯。",
                max_tokens=max(12000, batch_count * 500)
            )
            
            # 尝试多种分隔符解析
            chapters = []
            for sep in [r'\n###\s*', r'\n##\s*', r'第\d+章']:
                parts = re.split(sep, response)
                parts = [p.strip() for p in parts if p.strip()]
                # 筛出以"第X章"开头的
                chapters = [p for p in parts if re.match(r'第\d+章', p)]
                if chapters:
                    break
            
            chapters = chapters[:batch_count]
            
            if len(chapters) < batch_count:
                self._log(f"⚠️ 大纲数量不足 ({len(chapters)}/{batch_count})，重试...")
                self._log(f"   （原始响应首200字: {response[:200]}）")
                if attempt < max_attempts - 1:
                    continue
            
            return chapters
        
        raise Exception(f"大纲生成失败（{ch_start}-{ch_end}章），已达到最大重试次数")
    
    def layer3_scene_breakdown(self) -> List[Dict]:
        """第3层：场景分解，需用户确认"""
        self._log("\n🔍 第3层：场景分解...")
        self._update_progress(25, "正在分解场景...")
        
        total_scenes_estimate = self.chapters_count * self.scenes_per_chapter
        
        chapter_summaries = []
        for i, outline in enumerate(self.chapter_outlines, 1):
            summary = f"第{i}章: {outline}"
            chapter_summaries.append(summary)
        
        prompt = f"""
        将每一章拆解成具体的写作场景，场景描述要足够详细，为后续写作提供充分素材。

        【设定摘要】
        {self.setting_bible}

        【章节大纲】
        {"\n".join(chapter_summaries)}

        输出JSON数组，格式：
        [
          {{
            "chapter": 1,
            "scene_id": 1,
            "word_target": {self.words_per_scene},
            "summary": "详细描述（100-200字）：包含关键情节推进、角色动作及对话要点、场景氛围基调",
            "characters": ["角色名"],
            "emotion": "情绪标签",
            "location": "具体地点及环境特征"
          }}
        ]

        要求：
        - summary 必须足够详细，写明「发生了什么」「角色做了什么/说了什么」「氛围如何」
        - 每个场景的 summary 至少100字，为写作者提供充足素材
        - 总场景数约{total_scenes_estimate}个，只输出JSON数组。
        """
        
        max_attempts = 3
        for attempt in range(max_attempts):
            response = self.call_grok(prompt, system="只输出合法的JSON数组。", max_tokens=max(20000, total_scenes_estimate * 150))
            
            try:
                if "```json" in response:
                    response = response.split("```json")[1].split("```")[0]
                elif "```" in response:
                    response = response.split("```")[1].split("```")[0]
                scenes = json.loads(response)
                
                # 格式化显示
                formatted = json.dumps(scenes, ensure_ascii=False, indent=2)
                
                confirmed = self._confirm_with_user(
                    "确认场景分解",
                    formatted,
                    f"请确认或修改以上{len(scenes)}个场景的分解。\n5秒后自动确认。"
                )

                if confirmed == "__REGENERATE__":
                    self._log(f"🔄 用户要求重新生成场景分解（第{attempt+2}次尝试）")
                    continue
                else:
                    # 解析确认后的JSON并验证结构
                    try:
                        self.scenes = json.loads(confirmed)
                        if not self._validate_scenes_json(self.scenes):
                            self._log(f"⚠️ 场景结构验证失败，重试...")
                            if attempt < max_attempts - 1:
                                continue
                            else:
                                self.scenes = self._generate_fallback_scenes()
                    except json.JSONDecodeError as e:
                        self._log(f"⚠️ JSON解析失败: {e}，使用备用方案")
                        self.scenes = self._generate_fallback_scenes()

                    self._save_state("layer4")
                    self._log(f"✅ 场景分解已确认，共{len(self.scenes)}个场景")
                    return self.scenes
                    
            except json.JSONDecodeError as e:
                self._log(f"⚠️ JSON解析失败: {e}")
                if attempt == max_attempts - 1:
                    # 最后一次尝试，使用简化版本
                    self.scenes = self._generate_fallback_scenes()
                    return self.scenes
        
        self.scenes = self._generate_fallback_scenes()
        return self.scenes
    
    def _parse_chapters_from_text(self, text: str, ch_start: int, ch_end: int) -> Dict[int, str]:
        """从文本中解析章节，更健壮的方法"""
        chapters = {}
        parts = re.split(r'@@第(\d+)章@@', text)

        for i in range(1, len(parts), 2):
            try:
                ch_num = int(parts[i])
                ch_text = parts[i + 1].strip() if i + 1 < len(parts) else ""

                if ch_start <= ch_num <= ch_end and ch_text:
                    chapters[ch_num] = ch_text
            except (ValueError, IndexError):
                continue

        # 如果没有解析出任何章节，整体作为第一章
        if not chapters and text.strip():
            chapters[ch_start] = text.strip()

        return chapters

    def _validate_scenes_json(self, scenes: List[Dict]) -> bool:
        """验证场景 JSON 结构"""
        required_keys = {"chapter", "scene_id", "word_target", "summary"}
        for scene in scenes:
            if not isinstance(scene, dict):
                return False
            if not required_keys.issubset(scene.keys()):
                return False
            if not isinstance(scene.get("chapter"), int) or scene["chapter"] < 1:
                return False
            if not isinstance(scene.get("summary"), str) or len(scene["summary"]) < 10:
                return False
        return len(scenes) > 0

    def _count_words(self, text: str) -> int:
        """准确计算中文字数（中文字符 + 英文单词）"""
        chinese_count = len(re.findall(r'[一-鿿]', text))
        english_words = len(re.findall(r'\b[a-zA-Z]+\b', text))
        numbers = len(re.findall(r'\d+', text))
        return chinese_count + english_words + numbers

    def _get_dynamic_temperature(self, round_num: int, total_rounds: int) -> float:
        """根据迭代轮数动态调整温度参数"""
        if round_num == 1:
            return 0.85
        elif round_num < total_rounds:
            return 0.7
        else:
            return 0.5

    def _get_prev_context(self, prev_text: str, target_words: int) -> str:
        """动态计算前情回顾长度"""
        if not prev_text:
            return "（小说开头）"
        context_len = max(500, int(target_words * 0.4))
        return prev_text[-context_len:] if len(prev_text) > context_len else prev_text

    def _calculate_max_tokens(self, target_words: int, round_num: int, total_rounds: int) -> int:
        """精确计算所需 max_tokens"""
        base_tokens = int(target_words * 1.5)
        if round_num == 1:
            return int(base_tokens * 1.2)
        elif round_num < total_rounds:
            return int(base_tokens * 1.4)
        else:
            return int(base_tokens * 1.3)

    def _generate_fallback_scenes(self) -> List[Dict]:
        """两轮迭代写出 ch_start 到 ch_end 章"""
        num_chapters = ch_end - ch_start + 1
        total_target = self.words_per_chapter * num_chapters
        
        # 组装这批章的安排
        chunk_scenes = ""
        for c in range(ch_start, ch_end + 1):
            outline = ""
            if c <= len(self.chapter_outlines):
                outline = self.chapter_outlines[c - 1]
            scene_lines = [f"  {i+1}. {s.get('summary','')}"
                           for i, s in enumerate(chapter_scenes.get(c, []))]
            chunk_scenes += f"\n=== 第{c}章 ===\n大纲：{outline}\n场景安排：\n" + "\n".join(scene_lines) + "\n"
        
        current_text = ""
        current_len = 0
        
        for round_num in range(1, self.ROUNDS + 1):
            if not self.is_running:
                raise Exception("用户停止生成")
            
            self._update_progress(
                25 + int(((ch_start - 1 + (ch_end - ch_start + 1) * round_num / self.ROUNDS) / self.chapters_count) * 70),
                f"第{ch_start}-{ch_end}章 第{round_num}/{self.ROUNDS}轮"
            )
            
            if round_num == 1:
                per_chapter_target = self.words_per_chapter
                instruction = f"""写出第{ch_start}章到第{ch_end}章的初稿。每章必须达到{per_chapter_target}字以上，总计{total_target}字。确保情节连贯，按场景安排推进。"""
                temperature = self._get_dynamic_temperature(round_num, self.ROUNDS)
            else:
                shortfall = max(0, total_target - current_len)
                if shortfall > 0:
                    instruction = f"""上一轮只写了{current_len}字（目标{total_target}字），还差{shortfall}字！请在保持情节不变的前提下，从以下方面大幅扩充每章：
- 环境描写（详细刻画场景）
- 角色内心（想法、感受、回忆）
- 对话互动（每段对话至少3轮）
- 动作细节（具体的肢体语言）
- 氛围渲染
必须扩充到{total_target}字以上，不要删减任何已有内容。"""
                else:
                    instruction = f"润色文字，检查连贯性。保持{total_target}字以上。"
                temperature = self._get_dynamic_temperature(round_num, self.ROUNDS)
            
            prev_block = f"【上一轮内容】\n{current_text}" if current_text else ""

            # 使用动态前情回顾
            dynamic_prev_context = self._get_prev_context(prev_context, self.words_per_chapter)

            prompt = f"""你是专业小说家。请写出第{ch_start}章到第{ch_end}章的正文。

【核心设定】
{self.setting_bible}

【这批章的安排】
{chunk_scenes}

【前情回顾】
{dynamic_prev_context}

{prev_block}

【本轮任务】
{instruction}

【输出格式】
每章开头用「@@第N章@@」独占一行标记（N为章号），标记后直接写该章的正文。不要写章节标题。
"""
            
            system = "你是专业小说家，擅长长篇写作。每章必须字数达标，内容丰富。"
            
            response = self.call_grok(
                prompt,
                system=system,
                max_tokens=self._calculate_max_tokens(total_target, round_num, self.ROUNDS),
                temperature=temperature
            )
            current_text = response
            current_len = self._count_words(response)
            self._log(f"📝 第{ch_start}-{ch_end}章 第{round_num}/{self.ROUNDS}轮完成 ({current_len}字/目标{total_target}字)")
        
        # 拆分各章
        chapters_dict = self._parse_chapters_from_text(current_text, ch_start, ch_end)

        for ch_num, ch_text in chapters_dict.items():
            self.chapters[ch_num] = [ch_text]
            self._log(f"✅ 第{ch_num}章完成 ({len(ch_text)}字)")
            self._save_state("layer4", ch_num)

        # 检查是否有缺失的章节
        missing = [c for c in range(ch_start, ch_end + 1) if c not in chapters_dict]
        if missing:
            self._log(f"⚠️ 第{ch_start}-{ch_end}章中缺失: {missing}")
    
    def layer4_write_scenes(self, start_chapter: int = 1) -> str:
        """第4层：批量写作 + 三轮迭代（动态分批）"""
        self._log(f"\n✍️ 第4层：批量写作（{self.chunk_size}章/批 × {self.ROUNDS}轮迭代）...")
        
        if not self.scenes:
            self.scenes = self._generate_fallback_scenes()
        
        # 按章分组场景
        chapter_scenes = {}
        for sc in self.scenes:
            ch = sc.get("chapter", 1)
            chapter_scenes.setdefault(ch, []).append(sc)
        
        # 初始化 chapters
        if start_chapter == 1 and not self.chapters:
            self.chapters = {i: [] for i in range(1, self.chapters_count + 1)}
        
        if start_chapter > 1:
            self._log(f"📂 续传：从第{start_chapter}章开始")
        
        for ch_start in range(start_chapter, self.chapters_count + 1, self.chunk_size):
            if self._backend_down:
                break  # 后端停机，优雅退出
            if not self.is_running:
                raise Exception("用户停止生成")
            
            ch_end = min(ch_start + self.chunk_size - 1, self.chapters_count)
            
            # 前情回顾
            prev = "（小说开头）"
            if ch_start > 1 and self.chapters.get(ch_start - 1) and self.chapters[ch_start - 1]:
                full_prev = self.chapters[ch_start - 1][0]
                prev = full_prev[-1000:] if len(full_prev) > 1000 else full_prev
            
            # 整批失败自动重试（最多2次），等限流恢复
            chunk_ok = False
            for retry in range(3):
                try:
                    self._write_chapter_chunk(ch_start, ch_end, chapter_scenes, prev)
                    chunk_ok = True
                    break
                except Exception as e:
                    if "用户停止生成" in str(e):
                        raise
                    err_str = str(e)
                    self._log(f"❌ 第{ch_start}-{ch_end}章失败 ({retry+1}/3): {err_str[:150]}")
                    # 后端下线 — 立即停止，不再重试其他批次
                    if "No healthy provider" in err_str or "no healthy provider" in err_str.lower():
                        self._log(f"🛑 后端不可用，停止生成。当前进度已保存，可稍后继续。")
                        self._backend_down = True
                        self.is_running = False
                        break
                    if retry < 2:
                        wait = 10 * (retry + 1)
                        self._log(f"⏳ {wait}秒后重试...")
                        time.sleep(wait)
            if not chunk_ok:
                self._log(f"⚠️ 第{ch_start}-{ch_end}章最终失败，将被跳过")
            
            time.sleep(0.3)
        
        # 检查缺失章节
        missing = [c for c in range(1, self.chapters_count + 1) if not self.chapters.get(c)]
        if missing:
            self._log(f"⚠️ 以下章节缺失: {missing}")
        
        return self._merge_to_novel()
    
    def _merge_to_novel(self) -> str:
        full_story = []
        for ch_num in range(1, self.chapters_count + 1):
            if self.chapters.get(ch_num) and self.chapters[ch_num]:
                chapter_text = self.chapters[ch_num][0]
                title = ""
                if ch_num <= len(self.chapter_outlines):
                    title_match = re.search(r'《([^》]+)》', self.chapter_outlines[ch_num-1])
                    if title_match:
                        title = f"《{title_match.group(1)}》"
                full_story.append(f"\n## 第{ch_num}章{title}\n\n{chapter_text}")
        return "\n\n".join(full_story)
    
    def run(self, start_stage: str = "layer1") -> str:
        try:
            if start_stage in ("layer1",):
                self.layer1_setting_bible()
            if not self.is_running: return ""
            
            if start_stage in ("layer1", "layer2"):
                self.layer2_chapter_outlines()
            if not self.is_running: return ""
            
            if start_stage in ("layer1", "layer2", "layer3"):
                self.layer3_scene_breakdown()
            if not self.is_running: return ""
            
            story = self.layer4_write_scenes(start_chapter=max(1, self._resume_chapter))
            
            if self._backend_down:
                self._log("⚠️ 因后端不可用提前结束，当前进度已保存，稍后可继续。")
                return self._merge_to_novel()
            
            self._update_progress(100, "完成！")
            self._log(f"\n🎉 生成完成！共调用 {self.call_count} 次")
            self._save_state("done")
            
            return story
        except Exception as e:
            if str(e) != "用户停止生成":
                self._log(f"❌ 生成失败: {e}")
            raise


# ========== GUI界面 ==========
class NovelGeneratorGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("小说自动生成器 - Grok版（带用户确认）")
        self.root.geometry("900x750")
        
        self.config = load_config()
        self.generator = None
        self.generation_thread = None
        self.is_generating = False
        
        # 用于同步确认对话框
        self.confirm_result = None
        self.confirm_event = threading.Event()
        
        self._setup_ui()
        
        # 回填保存的章节设定
        saved_cc = self.config.get("chapters_count", "")
        saved_wpc = self.config.get("words_per_chapter", "")
        if saved_cc:
            self.chapters_count_var.set(saved_cc)
        if saved_wpc:
            self.words_per_chapter_var.set(saved_wpc)
        
        # 窗口关闭时确保停止生成
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
    
    def _setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # ===== API配置区域 =====
        config_frame = ttk.LabelFrame(main_frame, text="API配置", padding="10")
        config_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(config_frame, text="Base URL:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.base_url_var = tk.StringVar(value=self.config.get("base_url", "https://api.x.ai/v1"))
        self.base_url_entry = ttk.Entry(config_frame, textvariable=self.base_url_var, width=60)
        self.base_url_entry.grid(row=0, column=1, sticky=tk.W+tk.E)

        ttk.Label(config_frame, text="API Key:").grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=(10, 0))
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=60, show="*")
        self.api_key_entry.grid(row=1, column=1, sticky=tk.W+tk.E, pady=(10, 0))
        
        ttk.Label(config_frame, text="Model:").grid(row=2, column=0, sticky=tk.W, padx=(0, 10), pady=(10, 0))
        
        model_row = ttk.Frame(config_frame)
        model_row.grid(row=2, column=1, sticky=tk.W+tk.E, pady=(10, 0))
        self.model_var = tk.StringVar(value=self.config.get("model", "grok-4-1-fast-non-reasoning"))
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, width=50)
        self.model_combo.pack(side=tk.LEFT)
        ttk.Button(model_row, text="获取模型列表", command=self._fetch_models).pack(side=tk.LEFT, padx=(5, 0))
        
        button_frame = ttk.Frame(config_frame)
        button_frame.grid(row=3, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(button_frame, text="保存配置", command=self._save_config).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(button_frame, text="测试连接", command=self._test_connection).pack(side=tk.LEFT)
        
        config_frame.columnconfigure(1, weight=1)
        
        # ===== 输入区域 =====
        input_frame = ttk.LabelFrame(main_frame, text="小说设定", padding="10")
        input_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        ttk.Label(input_frame, text="主题（一句话创意）:").pack(anchor=tk.W)
        self.theme_text = scrolledtext.ScrolledText(input_frame, height=3, wrap=tk.WORD)
        self.theme_text.pack(fill=tk.X, pady=(5, 10))
        self.theme_text.insert(tk.END, self.config.get("theme", "一个普通图书管理员发现一本可以改写现实的无字之书，但每次改写都需要付出等价的记忆作为代价"))
        
        ttk.Label(input_frame, text="要求（字数、风格、特殊需求等）:").pack(anchor=tk.W)
        self.req_text = scrolledtext.ScrolledText(input_frame, height=5, wrap=tk.WORD)
        self.req_text.pack(fill=tk.X, pady=(5, 10))
        self.req_text.insert(tk.END, self.config.get("requirements", "5万字，第三人称，风格温暖治愈但有悬疑感，主角为女性，有3个主要配角"))
        
        # 章节设定
        chapter_frame = ttk.Frame(input_frame)
        chapter_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(chapter_frame, text="章节数（留空自动）:").pack(side=tk.LEFT)
        self.chapters_count_var = tk.StringVar()
        ttk.Entry(chapter_frame, textvariable=self.chapters_count_var, width=8).pack(side=tk.LEFT, padx=(2, 20))
        ttk.Label(chapter_frame, text="每章字数（留空默认1200）:").pack(side=tk.LEFT)
        self.words_per_chapter_var = tk.StringVar()
        ttk.Entry(chapter_frame, textvariable=self.words_per_chapter_var, width=8).pack(side=tk.LEFT, padx=(2, 0))
        
        # ===== 输出区域 =====
        output_frame = ttk.LabelFrame(main_frame, text="生成日志", padding="10")
        output_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        self.log_text = scrolledtext.ScrolledText(output_frame, height=10, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 进度条
        self.progress_var = tk.IntVar()
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 10))
        self.progress_label = ttk.Label(main_frame, text="就绪")
        self.progress_label.pack()
        
        # 控制按钮
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X)
        
        self.start_btn = ttk.Button(control_frame, text="开始生成", command=self._start_generation)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        # 断点续传按钮（有状态文件时才显示）
        self.resume_btn = ttk.Button(control_frame, text="继续上次生成", command=self._load_and_resume)
        if os.path.exists(STATE_FILE):
            self.resume_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.stop_btn = ttk.Button(control_frame, text="停止", command=self._stop_generation, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(control_frame, text="清空日志", command=self._clear_log).pack(side=tk.LEFT)
        ttk.Button(control_frame, text="保存日志", command=self._save_log).pack(side=tk.LEFT)

        # 为所有文本框和输入框添加右键菜单
        for widget in [self.theme_text, self.req_text, self.log_text, self.base_url_entry, self.api_key_entry]:
            widget.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        """显示右键菜单"""
        widget = event.widget
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: self._select_all(widget))
        menu.add_command(label="删除", command=lambda: self._delete_all(widget))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _select_all(self, widget):
        """全选文本"""
        try:
            if isinstance(widget, tk.Entry):
                widget.select_range(0, tk.END)
                widget.icursor(tk.END)
            else:
                widget.tag_add(tk.SEL, "1.0", tk.END)
                widget.mark_set(tk.INSERT, "1.0")
                widget.see(tk.INSERT)
        except:
            pass

    def _delete_all(self, widget):
        """删除所有文本"""
        try:
            if isinstance(widget, tk.Entry):
                widget.delete(0, tk.END)
            else:
                widget.delete("1.0", tk.END)
        except:
            pass

    def _save_config(self):
        self.config = {
            "api_key": self.api_key_var.get(),
            "base_url": self.base_url_var.get(),
            "model": self.model_var.get(),
            "chapters_count": self.chapters_count_var.get(),
            "words_per_chapter": self.words_per_chapter_var.get(),
            "theme": self.theme_text.get("1.0", tk.END).strip(),
            "requirements": self.req_text.get("1.0", tk.END).strip()
        }
        save_config(self.config)
        self._log("✅ 配置已保存")
    
    def _test_connection(self):
        api_key = self.api_key_var.get().strip()
        base_url = self.base_url_var.get().strip()
        model = self.model_var.get().strip()
        
        if not api_key:
            messagebox.showerror("错误", "请先填写API Key")
            return
        
        def test():
            try:
                client = OpenAI(api_key=api_key, base_url=base_url,
                                default_headers={"User-Agent": "Mozilla/5.0"})
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "回复'OK'"}],
                    max_tokens=10
                )
                resp_content = response.choices[0].message.content
                self.root.after(0, lambda r=resp_content: messagebox.showinfo("成功", f"连接成功！\n响应: {r}"))
            except Exception as exc:
                err_msg = str(exc)
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("失败", f"连接失败:\n{msg}"))
        
        threading.Thread(target=test, daemon=True).start()
        self._log("🔌 正在测试连接...")
    
    def _fetch_models(self):
        """从API获取可用模型列表（直接HTTP请求，不走OpenAI client）"""
        api_key = self.api_key_var.get().strip()
        base_url = self.base_url_var.get().strip().rstrip("/")
        
        if not base_url:
            messagebox.showerror("错误", "请填写Base URL")
            return
        
        # 拼接 /models 端点
        models_url = f"{base_url}/models"
        
        def fetch():
            try:
                req = urllib.request.Request(models_url)
                req.add_header("Accept", "application/json")
                req.add_header("User-Agent", "Mozilla/5.0")
                if api_key:
                    req.add_header("Authorization", f"Bearer {api_key}")
                
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                
                # 兼容 OpenAI 格式: {"object": "list", "data": [{"id": "xxx"}, ...]}
                if isinstance(data, dict) and "data" in data:
                    model_ids = sorted([m["id"] for m in data["data"]])
                elif isinstance(data, list):
                    model_ids = sorted([m["id"] if isinstance(m, dict) else str(m) for m in data])
                else:
                    raise ValueError(f"未识别的返回格式: {type(data)}")
                
                self.root.after(0, lambda ids=model_ids: self._update_model_list(ids))
            except urllib.error.HTTPError as exc:
                # 读取服务器返回的具体错误信息
                try:
                    body = exc.read().decode("utf-8")
                    detail = json.loads(body)
                    err_msg = detail.get("error", {}).get("message", body)
                except:
                    err_msg = f"HTTP {exc.code}: {exc.reason}"
                self.root.after(0, lambda msg=err_msg: self._log(f"⚠️ 获取模型列表失败: {msg}"))
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("失败", f"获取模型列表失败:\n{msg}"))
            except Exception as exc:
                err_msg = str(exc)
                self.root.after(0, lambda msg=err_msg: self._log(f"⚠️ 获取模型列表失败: {msg}"))
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("失败", f"获取模型列表失败:\n{msg}"))
        
        threading.Thread(target=fetch, daemon=True).start()
        self._log(f"🔄 正在获取模型列表 ({models_url})...")
    
    def _update_model_list(self, model_ids: list):
        """更新Model下拉框的候选列表"""
        self.model_combo["values"] = model_ids
        self._log(f"✅ 获取到 {len(model_ids)} 个模型")
        # 如果当前模型不在列表中，自动选择第一个
        if self.model_var.get() not in model_ids and model_ids:
            self.model_var.set(model_ids[0])
            self._log(f"📌 自动选择模型: {model_ids[0]}")
    
    def _log(self, message: str):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)
    
    def _save_log(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"log_{timestamp}.txt"
        content = self.log_text.get("1.0", tk.END)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        self._log(f"📄 日志已保存: {filename}")
    
    def _on_generator_save_state(self, state: dict):
        """NovelGenerator 回调：保存断点状态到文件"""
        save_resume_state(state)
        # 显示续传按钮
        if not self.resume_btn.winfo_ismapped():
            self.resume_btn.pack(side=tk.LEFT, padx=(0, 10), before=self.stop_btn)
    
    def _load_and_resume(self):
        """从断点恢复继续生成"""
        state = load_resume_state()
        if not state:
            messagebox.showinfo("提示", "没有可恢复的生成状态")
            return
        
        stage = state.get("stage", "layer1")
        stage_names = {"layer1": "设定圣经", "layer2": "章节大纲", "layer3": "场景分解", "layer4": "批量写作", "done": "已完成"}
        stage_name = stage_names.get(stage, stage)
        
        if stage == "done":
            if not messagebox.askyesno("提示", "上次生成已完成。是否重新开始？"):
                return
            stage = "layer1"
            clear_resume_state()
        else:
            if not messagebox.askyesno("确认续传", f"将从「{stage_name}」阶段继续生成。\n(API调用计数: {state.get('call_count', 0)})\n\n确认恢复？"):
                return
        
        # 填充 UI 字段
        self.theme_text.delete("1.0", tk.END)
        self.theme_text.insert(tk.END, state.get("theme", ""))
        self.req_text.delete("1.0", tk.END)
        self.req_text.insert(tk.END, state.get("requirements", ""))
        
        config = state.get("config", {})
        self.api_key_var.set(config.get("api_key", ""))
        self.base_url_var.set(config.get("base_url", ""))
        self.model_var.set(config.get("model", ""))
        
        self._start_generation(resume_from=stage)
    
    def _update_progress(self, value: int, text: str):
        self.progress_var.set(value)
        self.progress_label.config(text=text)
        self.root.update_idletasks()
    
    def _show_confirm_dialog(self, title: str, content: str, prompt: str) -> str:
        """显示确认对话框，返回用户确认后的内容"""
        self.confirm_result = None
        self.confirm_event.clear()
        self.active_dialog = None
        
        # 在主线程中创建对话框
        self.root.after(0, lambda: self._create_confirm_dialog_safe(title, content, prompt))
        
        # 等对话框创建（最多10秒），超时说明 after 回调未触发
        for _ in range(20):
            if self.confirm_event.is_set():
                return self.confirm_result
            if self.active_dialog is not None:
                break
            time.sleep(0.5)
        else:
            self._log("⚠️ 确认对话框创建失败（10秒超时）")
            return "__CANCEL__"
        
        # 对话框已打开，无限等待用户操作（对话框自身有倒计时自动确认机制）
        self.confirm_event.wait()
        return self.confirm_result
    
    def _create_confirm_dialog_safe(self, title: str, content: str, prompt: str):
        """安全创建确认对话框，捕获异常"""
        try:
            self._create_confirm_dialog(title, content, prompt)
        except Exception as e:
            self._log(f"❌ 创建确认对话框失败: {e}")
            self.confirm_result = "__CANCEL__"
            self.confirm_event.set()
    
    def _create_confirm_dialog(self, title: str, content: str, prompt: str):
        """创建确认对话框（在主线程中执行）"""
        dialog = ConfirmDialog(self.root, title, content, prompt)
        self.active_dialog = dialog  # 记录当前对话框
        result = dialog.get_result()
        self.active_dialog = None
        self.confirm_result = result
        self.confirm_event.set()
    
    def _start_generation(self, resume_from: str = None):
        theme = self.theme_text.get("1.0", tk.END).strip()
        requirements = self.req_text.get("1.0", tk.END).strip()
        api_key = self.api_key_var.get().strip()
        base_url = self.base_url_var.get().strip()
        model = self.model_var.get().strip()
        
        if not theme:
            messagebox.showerror("错误", "请输入小说主题")
            return
        if not api_key:
            messagebox.showerror("错误", "请填写API Key")
            return
        
        # 非续传时清除旧状态
        resume_state = None
        start_stage = "layer1"
        if resume_from:
            resume_state = load_resume_state()
            start_stage = resume_from
        
        self._save_config()
        # 读取章节设定
        chapters_count = None
        words_per_chapter = None
        try:
            val = self.chapters_count_var.get().strip()
            if val:
                chapters_count = int(val)
                if chapters_count < 1:
                    messagebox.showerror("错误", "章节数必须大于0")
                    return
        except ValueError:
            messagebox.showerror("错误", "章节数必须是整数")
            return

        try:
            val = self.words_per_chapter_var.get().strip()
            if val:
                words_per_chapter = int(val)
                if words_per_chapter < 100:
                    messagebox.showerror("错误", "每章字数必须至少100字")
                    return
        except ValueError:
            messagebox.showerror("错误", "每章字数必须是整数")
            return
        # 续传时不删日志（追加模式）
        if not resume_from:
            self.log_text.delete("1.0", tk.END)
        else:
            self._log(f"\n{'='*40}")
            self._log(f"📂 从「{resume_from}」阶段继续生成")
        
        self.is_generating = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress_var.set(0)
        
        def run():
            try:
                self.generator = NovelGenerator(
                    theme=theme,
                    requirements=requirements,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    progress_callback=self._update_progress,
                    log_callback=self._log,
                    confirm_callback=self._show_confirm_dialog,
                    parent_window=self.root,
                    resume_state=resume_state,
                    state_callback=self._on_generator_save_state,
                    chapters_count=chapters_count,
                    words_per_chapter=words_per_chapter
                )
                
                story = self.generator.run(start_stage=start_stage)
                
                if story:
                    self.root.after(0, lambda s=story: self._save_story(s))
                
            except Exception as exc:
                err_msg = str(exc)
                self.root.after(0, lambda msg=err_msg: self._log(f"❌ 错误: {msg}"))
            finally:
                self.root.after(0, self._generation_finished)
        
        self.generation_thread = threading.Thread(target=run, daemon=True)
        self.generation_thread.start()
    
    def _stop_generation(self):
        # 防连点
        if not self.is_generating:
            return
        # 强关当前确认对话框
        if hasattr(self, 'active_dialog') and self.active_dialog:
            self.active_dialog.result = "__CANCEL__"
            try:
                self.active_dialog.dialog.grab_release()
                self.active_dialog.dialog.destroy()
            except:
                pass
        if self.generator:
            self.generator.stop()
        self._log("⚠️ 正在停止生成（进行中的API调用需等返回后才会中止）...")
        self.stop_btn.config(state=tk.DISABLED)  # 禁用停止按钮防连点
    
    def _generation_finished(self):
        self.is_generating = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        # 完成后清理状态，隐藏续传按钮
        state = load_resume_state()
        if state and state.get("stage") == "done":
            clear_resume_state()
            if self.resume_btn.winfo_ismapped():
                self.resume_btn.pack_forget()
    
    def _save_story(self, story: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"novel_{timestamp}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(story)
        self._log(f"✅ 小说已保存: {filename}")
        messagebox.showinfo("完成", f"生成完成！\n已保存到: {filename}")
    
    def _on_close(self):
        """关闭窗口时停止生成"""
        self._stop_generation()
        self.root.destroy()
    
    def run(self):
        self.root.mainloop()


# ========== 入口 ==========
if __name__ == "__main__":
    app = NovelGeneratorGUI()
    app.run()