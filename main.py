import random
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any
import logging
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DIARY_JSON_FILE = Path("data/plugins_data/astrbot_plugin_dogdiary") / "dog_diaries.json"
SUMMARY_CACHE_FILE = Path("data/plugins_data/astrbot_plugin_dogdiary") / "summary_cache.json"
ORIGINAL_BACKUP_DIR = Path("data/plugins_data/astrbot_plugin_dogdiary/originals")

@register("astrbot_plugin_dogdiary", "大沙北", "每日一记的舔狗日记", "1.2.0", "https://github.com/bigshabei/astrbot_plugin_dogdiary")
class LickDogDiaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.diary_file = DIARY_JSON_FILE
        self.summary_cache_file = SUMMARY_CACHE_FILE
        self.original_backup_dir = ORIGINAL_BACKUP_DIR
        self._ensure_data_directory()
        self._initialize_files()
        self.min_word_count = config.get("dogdiary_min_word_count", 150) if config else 150
        self.max_word_count = config.get("dogdiary_max_word_count", 300) if config else 300
        self.diary_style = config.get("dogdiary_style", "幽默自嘲") if config else "幽默自嘲"
        self.auto_generate_time = config.get("dogdiary_auto_generate_time", "08:00") if config else "08:00"
        self.default_prompt = (f"请生成一篇{{style}}风格的舔狗日记，内容要反映出对心上人爱而不得的痛苦心情，"
                               f"字数在{{min_word_count}}到{{max_word_count}}字之间。日期为：{{date}}。"
                               f"请考虑之前的日记内容：{{history}}")
        self.summary_cache: Dict[str, str] = self._load_summary_cache()
        self.emotion_threshold = 7
        asyncio.create_task(self._daily_diary_task())
        logger.info(f"启动舔狗日记自动生成定时任务，时间设置为 {self.auto_generate_time}")

    def _ensure_data_directory(self):
        data_dir = DIARY_JSON_FILE.parent
        if not data_dir.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
        if not self.original_backup_dir.exists():
            self.original_backup_dir.mkdir(parents=True, exist_ok=True)

    def _initialize_files(self):
        try:
            if not self.diary_file.exists():
                self.diary_file.touch()
                with open(self.diary_file, 'w', encoding='utf-8') as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
            if not self.summary_cache_file.exists():
                self.summary_cache_file.touch()
                with open(self.summary_cache_file, 'w', encoding='utf-8') as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"初始化文件时出错: {e}")

    def _load_diaries(self) -> Dict[str, Any]:
        try:
            with open(self.diary_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载日记文件时出错: {e}")
            return {}

    def _save_diaries(self, diaries: Dict[str, Any]):
        try:
            with open(self.diary_file, 'w', encoding='utf-8') as f:
                json.dump(diaries, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存日记文件时出错: {e}")

    def _load_summary_cache(self) -> Dict[str, str]:
        try:
            with open(self.summary_cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载总结缓存文件时出错: {e}")
            return {}

    def _save_summary_cache(self, cache: Dict[str, str]):
        try:
            with open(self.summary_cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存总结缓存文件时出错: {e}")

    def _backup_original_diary(self, date_str: str, time_str: str, content: str):
        try:
            backup_file = self.original_backup_dir / f"diary_{date_str}.txt"
            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(f"【舔狗日记 - {time_str}】\n{content}\n")
            logger.info(f"已备份日记原文: {date_str}")
        except Exception as e:
            logger.error(f"备份日记原文时出错 (日期: {date_str}): {e}")

    def _load_original_diary(self, date_str: str) -> str:
        try:
            backup_file = self.original_backup_dir / f"diary_{date_str}.txt"
            if backup_file.exists():
                with open(backup_file, 'r', encoding='utf-8') as f:
                    return f.read()
            return ""
        except Exception as e:
            logger.error(f"加载日记原文时出错 (日期: {date_str}): {e}")
            return ""

    async def _analyze_emotion_intensity(self, content: str) -> int:
        try:
            prompt = f"分析以下文本的情感强度（评分 1-10 分，1 表示情感极弱，10 表示情感极强），仅返回一个数字：\n{content[:500]}"
            llm_response = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None
            )
            if llm_response.role == "assistant":
                response_text = llm_response.completion_text.strip()
                try:
                    score = int(response_text)
                    if 1 <= score <= 10:
                        return score
                except ValueError:
                    logger.error(f"LLM 返回的情感强度评分无效: {response_text}")
            return 0
        except Exception as e:
            logger.error(f"分析情感强度时出错: {e}")
            return 0

    async def _daily_diary_task(self):
        logger.info(f"启动舔狗日记自动生成定时任务，时间设置为 {self.auto_generate_time}")
        while True:
            try:
                now = datetime.now()
                hour, minute = map(int, self.auto_generate_time.split(':'))
                next_trigger = datetime(now.year, now.month, now.day, hour, minute)
                if now > next_trigger:
                    next_trigger += timedelta(days=1)
                wait_seconds = (next_trigger - now).total_seconds()
                await asyncio.sleep(wait_seconds)
                
                today = date.today().isoformat()
                diaries = self._load_diaries()
                if today in diaries:
                    logger.info(f"今天 ({today}) 已生成日记，跳过自动生成")
                    continue
                
                current_time = datetime.now().strftime("%Y-%m-%d")
                weekday = datetime.now().strftime("%w")
                weather = random.choice(['☀️', '🌥', '🌧', '🌪'])
                weekdays = ['日', '一', '二', '三', '四', '五', '六']
                weekday_cn = weekdays[int(weekday)]
                date_info = f"{current_time} {weather}周{weekday_cn}"
                
                previous_diary_summary = await self.summarize_and_forget_diaries(diaries)
                
                prompt = self.default_prompt.format(
                    style=self.diary_style,
                    min_word_count=self.min_word_count,
                    max_word_count=self.max_word_count,
                    date=date_info,
                    history=previous_diary_summary if previous_diary_summary else '暂无历史记录'
                )
                
                max_attempts = 3
                diary_content = None
                for attempt in range(max_attempts):
                    try:
                        llm_response = await self.context.get_using_provider().text_chat(
                            prompt=prompt,
                            contexts=[],
                            func_tool=None
                        )
                        if llm_response.role == "assistant":
                            diary_content = llm_response.completion_text.strip()
                            break
                    except Exception as e:
                        logger.error(f"自动生成日记时出错 (尝试 {attempt+1}/{max_attempts}): {e}")
                        if attempt == max_attempts - 1:
                            logger.error("自动生成日记失败，等待下一天重试")
                            break
                
                if diary_content:
                    time_str = f"{current_time} {weather}周{weekday_cn}"
                    emotion_score = await self._analyze_emotion_intensity(diary_content)
                    is_important = emotion_score >= self.emotion_threshold
                    if emotion_score > 0:
                        logger.info(f"自动生成日记情感强度评分: {emotion_score}, 标记为重要: {is_important}")
                    else:
                        logger.warning("情感强度分析失败，默认不标记为重要")
                        is_important = False
                    diaries[today] = {'time': time_str, 'content': diary_content, 'important': is_important, 'emotion_score': emotion_score}
                    self._save_diaries(diaries)
                    self._backup_original_diary(today, time_str, diary_content)
                    logger.info(f"自动生成日记成功: {today}")
                else:
                    logger.error("自动生成日记内容为空，跳过保存")
            except Exception as e:
                logger.error(f"自动生成日记任务异常: {e}")
                await asyncio.sleep(3600)

    async def _generate_diary_for_today(self, diaries: Dict[str, Any], today: str):
        try:
            current_time = datetime.now().strftime("%Y-%m-%d")
            weekday = datetime.now().strftime("%w")
            weather = random.choice(['☀️', '🌥', '🌧', '🌪'])
            weekdays = ['日', '一', '二', '三', '四', '五', '六']
            weekday_cn = weekdays[int(weekday)]
            date_info = f"{current_time} {weather}周{weekday_cn}"
            
            previous_diary_summary = await self.summarize_and_forget_diaries(diaries)
            
            prompt = self.default_prompt.format(
                style=self.diary_style,
                min_word_count=self.min_word_count,
                max_word_count=self.max_word_count,
                date=date_info,
                history=previous_diary_summary if previous_diary_summary else '暂无历史记录'
            )
            
            max_attempts = 3
            diary_content = None
            for attempt in range(max_attempts):
                try:
                    llm_response = await self.context.get_using_provider().text_chat(
                        prompt=prompt,
                        contexts=[],
                        func_tool=None
                    )
                    if llm_response.role == "assistant":
                        diary_content = llm_response.completion_text.strip()
                        break
                except Exception as e:
                    logger.error(f"生成日记时出错 (尝试 {attempt+1}/{max_attempts}): {e}")
                    if attempt == max_attempts - 1:
                        logger.error("生成日记失败")
                        return
            
            if diary_content:
                time_str = f"{current_time} {weather}周{weekday_cn}"
                emotion_score = await self._analyze_emotion_intensity(diary_content)
                is_important = emotion_score >= self.emotion_threshold
                if emotion_score > 0:
                    logger.info(f"日记情感强度评分: {emotion_score}, 标记为重要: {is_important}")
                else:
                    logger.warning("情感强度分析失败，默认不标记为重要")
                    is_important = False
                diaries[today] = {'time': time_str, 'content': diary_content, 'important': is_important, 'emotion_score': emotion_score}
                self._save_diaries(diaries)
                self._backup_original_diary(today, time_str, diary_content)
                logger.info(f"成功生成日记: {today}")
            else:
                logger.error("生成日记内容为空，跳过保存")
        except Exception as e:
            logger.error(f"生成当天日记时发生异常: {e}")

    @filter.command("今日舔狗日记")
    async def generate_diary(self, event: AstrMessageEvent):
        today = date.today().isoformat()
        
        diaries = self._load_diaries()
        if not diaries and diaries != {}:
            yield event.plain_result("读取日记文件失败，请稍后重试。")
            return
        
        if today in diaries:
            yield event.plain_result(f"【今日舔狗日记 - {diaries[today]['time']}】\n{diaries[today]['content']}")
            return
        
        current_time = datetime.now().strftime("%Y-%m-%d")
        weekday = datetime.now().strftime("%w")
        weather = random.choice(['☀️', '🌥', '🌧', '🌪'])
        weekdays = ['日', '一', '二', '三', '四', '五', '六']
        weekday_cn = weekdays[int(weekday)]
        date_info = f"{current_time} {weather}周{weekday_cn}"
        
        previous_diary_summary = await self.summarize_and_forget_diaries(diaries)
        
        prompt = self.default_prompt.format(
            style=self.diary_style,
            min_word_count=self.min_word_count,
            max_word_count=self.max_word_count,
            date=date_info,
            history=previous_diary_summary if previous_diary_summary else '暂无历史记录'
        )
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                llm_response = await self.context.get_using_provider().text_chat(
                    prompt=prompt,
                    contexts=[],
                    func_tool=None
                )
                if llm_response.role == "assistant":
                    diary_content = llm_response.completion_text.strip()
                    time_str = f"{current_time} {weather}周{weekday_cn}"
                    emotion_score = await self._analyze_emotion_intensity(diary_content)
                    is_important = emotion_score >= self.emotion_threshold
                    if emotion_score > 0:
                        logger.info(f"日记情感强度评分: {emotion_score}, 标记为重要: {is_important}")
                    else:
                        logger.warning("情感强度分析失败，默认不标记为重要")
                        is_important = False
                    diaries[today] = {'time': time_str, 'content': diary_content, 'important': is_important, 'emotion_score': emotion_score}
                    self._save_diaries(diaries)
                    self._backup_original_diary(today, time_str, diary_content)
                    result_msg = f"【今日舔狗日记 - {diaries[today]['time']}】\n{diary_content}"
                    if emotion_score > 0:
                        result_msg += f"\n(情感强度: {emotion_score}/10)"
                    yield event.plain_result(result_msg)
                    return
                else:
                    yield event.plain_result("生成日记失败，请稍后重试。")
                    return
            except Exception as e:
                logger.error(f"调用 LLM 生成日记时出错 (尝试 {attempt+1}/{max_attempts}): {e}")
                if attempt == max_attempts - 1:
                    yield event.plain_result("生成日记时发生错误，请稍后重试。")
                continue

    @filter.command("舔狗日记")
    async def temporary_diary(self, event: AstrMessageEvent):
        current_time = datetime.now().strftime("%Y-%m-%d")
        weekday = datetime.now().strftime("%w")
        weather = random.choice(['☀️', '🌥', '🌧', '🌪'])
        weekdays = ['日', '一', '二', '三', '四', '五', '六']
        weekday_cn = weekdays[int(weekday)]
        date_info = f"{current_time} {weather}周{weekday_cn}"
        
        diaries = self._load_diaries()
        if not diaries and diaries != {}:
            yield event.plain_result("读取日记文件失败，请稍后重试。")
            return
        
        previous_diary_summary = await self.summarize_and_forget_diaries(diaries)
        
        prompt = self.default_prompt.format(
            style=self.diary_style,
            min_word_count=self.min_word_count,
            max_word_count=self.max_word_count,
            date=date_info,
            history=previous_diary_summary if previous_diary_summary else '暂无历史记录'
        )
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                llm_response = await self.context.get_using_provider().text_chat(
                    prompt=prompt,
                    contexts=[],
                    func_tool=None
                )
                if llm_response.role == "assistant":
                    diary_content = llm_response.completion_text.strip()
                    time_str = f"{current_time} {weather}周{weekday_cn}"
                    emotion_score = await self._analyze_emotion_intensity(diary_content)
                    if emotion_score > 0:
                        logger.info(f"临时日记情感强度评分: {emotion_score}")
                    result_msg = f"【临时舔狗日记 - {time_str}】\n{diary_content}"
                    if emotion_score > 0:
                        result_msg += f"\n(情感强度: {emotion_score}/10)"
                    yield event.plain_result(result_msg)
                    return
                else:
                    yield event.plain_result("生成临时日记失败，请稍后重试。")
                    return
            except Exception as e:
                logger.error(f"调用 LLM 生成临时日记时出错 (尝试 {attempt+1}/{max_attempts}): {e}")
                if attempt == max_attempts - 1:
                    yield event.plain_result("生成临时日记时发生错误，请稍后重试。")
                continue

    @filter.command("舔狗日记列表")
    async def list_diaries(self, event: AstrMessageEvent):
        diaries = self._load_diaries()
        if not diaries:
            yield event.plain_result("暂无日记记录。")
            return
        
        diary_list = []
        sorted_diaries = sorted(diaries.items(), key=lambda x: date.fromisoformat(x[0]), reverse=True)
        for diary_date, diary in sorted_diaries:
            important_mark = "⭐" if diary.get('important', False) else ""
            emotion_score = diary.get('emotion_score', 'N/A')
            diary_list.append(f"{diary_date} - {diary['time'].split(' ')[1]} {important_mark} (情感强度: {emotion_score}/10)")
        
        yield event.plain_result(f"【舔狗日记列表】\n" + "\n".join(diary_list))

    @filter.command("重写舔狗日记")
    async def rewrite_diary(self, event: AstrMessageEvent):
        today = date.today().isoformat()
        diaries = self._load_diaries()
        
        yield event.plain_result("正在重写今天的舔狗日记...")
        current_time = datetime.now().strftime("%Y-%m-%d")
        weekday = datetime.now().strftime("%w")
        weather = random.choice(['☀️', '🌥', '🌧', '🌪'])
        weekdays = ['日', '一', '二', '三', '四', '五', '六']
        weekday_cn = weekdays[int(weekday)]
        date_info = f"{current_time} {weather}周{weekday_cn}"
        
        previous_diary_summary = await self.summarize_and_forget_diaries(diaries)
        prompt = self.default_prompt.format(
            style=self.diary_style,
            min_word_count=self.min_word_count,
            max_word_count=self.max_word_count,
            date=date_info,
            history=previous_diary_summary if previous_diary_summary else '暂无历史记录'
        )
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                llm_response = await self.context.get_using_provider().text_chat(
                    prompt=prompt,
                    contexts=[],
                    func_tool=None
                )
                if llm_response.role == "assistant":
                    diary_content = llm_response.completion_text.strip()
                    time_str = f"{current_time} {weather}周{weekday_cn}"
                    emotion_score = await self._analyze_emotion_intensity(diary_content)
                    is_important = emotion_score >= self.emotion_threshold
                    if emotion_score > 0:
                        logger.info(f"重写日记情感强度评分: {emotion_score}, 标记为重要: {is_important}")
                    else:
                        logger.warning("情感强度分析失败，默认不标记为重要")
                        is_important = False
                    diaries[today] = {'time': time_str, 'content': diary_content, 'important': is_important, 'emotion_score': emotion_score}
                    self._save_diaries(diaries)
                    self._backup_original_diary(today, time_str, diary_content)
                    result_msg = f"【重写舔狗日记 - {diaries[today]['time']}】\n{diary_content}"
                    if emotion_score > 0:
                        result_msg += f"\n(情感强度: {emotion_score}/10)"
                    yield event.plain_result(result_msg)
                    return
                else:
                    yield event.plain_result("重写日记失败，请稍后重试。")
                    return
            except Exception as e:
                logger.error(f"调用 LLM 重写日记时出错 (尝试 {attempt+1}/{max_attempts}): {e}")
                if attempt == max_attempts - 1:
                    yield event.plain_result("重写日记时发生错误，请稍后重试。")
                continue

    @filter.command("舔狗帮助")
    async def help_command(self, event: AstrMessageEvent):
        help_text = (
            "【舔狗日记插件帮助】\n"
            "这是一个生成幽默舔狗日记的插件，模拟人类记忆遗忘机制。\n\n"
            "可用指令列表：\n"
            "- 今日舔狗日记：查看或生成当天的舔狗日记。\n"
            "- 舔狗日记：基于历史记忆临时生成一份舔狗日记，不保存。\n"
            "- 舔狗日记列表：列出所有日记的日期和天气信息。\n"
            "- 重写舔狗日记：重写当天的舔狗日记，覆盖原有内容。\n"
            "- 舔狗帮助：显示本帮助信息。\n\n"
            f"日记将每天在 {self.auto_generate_time} 自动生成。"
        )
        yield event.plain_result(help_text)

    async def summarize_and_forget_diaries(self, diaries: Dict[str, Any]) -> str:
        if not diaries:
            return ""
            
        today = date.today().isoformat()
        summaries = []
        sorted_diaries = sorted(diaries.items(), key=lambda x: date.fromisoformat(x[0]), reverse=True)
        
        cache_key = f"summary_{today}"
        if cache_key in self.summary_cache:
            logger.info("使用缓存的历史日记总结")
            return self.summary_cache[cache_key]
        
        for diary_date, diary in sorted_diaries:
            if diary_date == today:
                continue
            if diary.get('important', False):
                summaries.append(f"[重要日记 {diary_date}] {diary['content']}")
                continue
            days_diff = (date.fromisoformat(today) - date.fromisoformat(diary_date)).days
            if days_diff <= 7:
                summaries.append(f"[最近日记 {diary_date}] {diary['content']}")
            elif days_diff <= 30:
                cache_key_specific = f"summary_{diary_date}"
                if cache_key_specific in self.summary_cache:
                    summaries.append(self.summary_cache[cache_key_specific])
                else:
                    summary_prompt = f"请提取以下日记中的关键情感信息，不超过50字：\n{diary['content']}"
                    try:
                        llm_response = await self.context.get_using_provider().text_chat(
                            prompt=summary_prompt,
                            contexts=[],
                            func_tool=None
                        )
                        if llm_response.role == "assistant":
                            summary_text = llm_response.completion_text
                            summaries.append(f"[摘要 {diary_date}] {summary_text}")
                            self.summary_cache[cache_key_specific] = f"[摘要 {diary_date}] {summary_text}"
                    except Exception as e:
                        logger.error(f"总结日记时出错 (日期: {diary_date}): {e}")
                        summaries.append(f"[摘要 {diary_date}] 无法获取摘要")
            else:
                cache_key_specific = f"keywords_{diary_date}"
                if cache_key_specific in self.summary_cache:
                    summaries.append(self.summary_cache[cache_key_specific])
                else:
                    summary_prompt = f"请从以下日记中提取最多3个情感关键词：\n{diary['content']}"
                    try:
                        llm_response = await self.context.get_using_provider().text_chat(
                            prompt=summary_prompt,
                            contexts=[],
                            func_tool=None
                        )
                        if llm_response.role == "assistant":
                            summary_text = llm_response.completion_text
                            summaries.append(f"[关键词 {diary_date}] {summary_text}")
                            self.summary_cache[cache_key_specific] = f"[关键词 {diary_date}] {summary_text}"
                    except Exception as e:
                        logger.error(f"提取关键词时出错 (日期: {diary_date}): {e}")
                        summaries.append(f"[关键词 {diary_date}] 无法获取关键词")
        
        result_summary = "\n".join(summaries) if summaries else "暂无历史记录"
        self.summary_cache[cache_key] = result_summary
        self._save_summary_cache(self.summary_cache)
        return result_summary

    async def terminate(self):
        logger.info("舔狗日记插件卸载中...")
