import random
import json
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List
import logging
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from aiocqhttp.exceptions import ActionFailed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DIARY_JSON_FILE = Path("data/plugins_data/astrbot_plugin_dogdiary") / "dog_diaries.json"
SUMMARY_CACHE_FILE = Path("data/plugins_data/astrbot_plugin_dogdiary") / "summary_cache.json"
ORIGINAL_BACKUP_DIR = Path("data/plugins_data/astrbot_plugin_dogdiary/originals")

# 转发阈值，超过此字数以转发样式发送
FORWARD_THRESHOLD = 500

@register("astrbot_plugin_dogdiary", "大沙北", "每日一记的舔狗日记", "1.3.6", "https://github.com/bigshabei/astrbot_plugin_dogdiary")
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
        self.auto_send_time = config.get("dogdiary_auto_send_time", "09:00") if config else "09:00"
        self.auto_send_groups = [str(gid) for gid in config.get("dogdiary_auto_send_groups", [])] if config else []
        self.default_prompt = (f"请生成一篇{{style}}风格的舔狗日记，内容要反映出对心上人爱而不得的痛苦心情，"
                               f"字数在{{min_word_count}}到{{max_word_count}}字之间。日期为：{{date}}。"
                               f"请考虑之前的日记内容：{{history}}")
        self.summary_cache: Dict[str, str] = self._load_summary_cache()
        self.emotion_threshold = 7
        asyncio.create_task(self._daily_diary_task())
        asyncio.create_task(self._daily_send_task())
        logger.info(f"启动舔狗日记自动生成定时任务，时间设置为 {self.auto_generate_time}")
        logger.info(f"启动舔狗日记自动发送定时任务，时间设置为 {self.auto_send_time}，发送群组: {self.auto_send_groups}")

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

    async def _daily_send_task(self):
        logger.info(f"启动舔狗日记自动发送定时任务，时间设置为 {self.auto_send_time}")
        while True:
            try:
                now = datetime.now()
                hour, minute = map(int, self.auto_send_time.split(':'))
                next_trigger = datetime(now.year, now.month, now.day, hour, minute)
                if now > next_trigger:
                    next_trigger += timedelta(days=1)
                wait_seconds = (next_trigger - now).total_seconds()
                await asyncio.sleep(wait_seconds)
                
                if not self.auto_send_groups:
                    logger.info("未设置自动发送的群组，跳过发送任务")
                    continue
                
                today = date.today().isoformat()
                diaries = self._load_diaries()
                if today not in diaries:
                    logger.info(f"今天 ({today}) 尚未生成日记，跳过自动发送")
                    continue
                
                diary_content = diaries[today]['content']
                time_str = diaries[today]['time']
                emotion_score = diaries[today].get('emotion_score', 0)
                result_msg = f"【今日舔狗日记 - {time_str}】\n{diary_content}"
                if emotion_score > 0:
                    result_msg += f"\n(情感强度: {emotion_score}/10)"
                
                # 检查是否需要以转发样式发送
                use_forward_style = len(result_msg) > FORWARD_THRESHOLD
                sent_count = 0
                
                # 获取所有平台实例，尝试找到支持发送消息的适配器
                platforms = self.context.platform_manager.get_insts()
                if not platforms:
                    logger.error("定时发送失败：未找到任何平台适配器。")
                    continue
                
                # 优先尝试获取 aiocqhttp 平台
                client = None
                for platform in platforms:
                    if platform.__class__.__name__.lower().find("aiocqhttp") != -1:
                        if hasattr(platform, 'client') and platform.client:
                            client = platform.client
                            logger.info("找到 AIOCQHTTP 平台适配器，用于定时发送。")
                            break
                if not client:
                    logger.warning("未找到 AIOCQHTTP 平台适配器，定时发送可能失败。")
                    continue
                
                # 尝试获取 bot 的 QQ 号作为 uin
                bot_uin = "743498954"  # 默认值
                try:
                    bot_info = await client.get_login_info()
                    if bot_info and 'user_id' in bot_info:
                        bot_uin = str(bot_info['user_id'])
                        logger.info(f"获取到 bot QQ 号: {bot_uin}")
                except Exception as e:
                    logger.warning(f"获取 bot QQ 号失败，使用默认值: {e}")
                
                for group_id in self.auto_send_groups:
                    try:
                        if use_forward_style:
                            # 以转发样式发送
                            forward_node = {
                                "type": "node",
                                "data": {
                                    "name": "舔狗日记",
                                    "uin": bot_uin,
                                    "content": [
                                        {"type": "text", "data": {"text": result_msg}}
                                    ]
                                }
                            }
                            await client.send_group_msg(group_id=int(group_id), message=[forward_node])
                        else:
                            await client.send_group_msg(group_id=int(group_id), message=result_msg)
                        logger.info(f"成功发送日记到群组 {group_id}, 样式: {'转发' if use_forward_style else '文本'}")
                        sent_count += 1
                        await asyncio.sleep(1)  # 防止发送过快被限制
                    except ActionFailed as e:
                        logger.error(f"发送日记到群组 {group_id} 失败 (ActionFailed): {e}")
                    except Exception as e:
                        logger.error(f"发送日记到群组 {group_id} 失败: {e}")
                
                if sent_count == 0:
                    logger.warning("没有成功发送日记到任何群组，请检查配置的群组ID是否正确。")
            except Exception as e:
                logger.error(f"自动发送日记任务异常: {e}")
                await asyncio.sleep(3600)  # 发生异常时等待1小时后重试

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
        
        message_str = event.message_str.strip()
        date_pattern = r'(\d{1,2})\.(\d{1,2})'
        match = re.search(date_pattern, message_str)
        
        if match:
            month = int(match.group(1))
            day = int(match.group(2))
            current_year = datetime.now().year
            target_date = f"{current_year:04d}-{month:02d}-{day:02d}"
            
            if target_date in diaries:
                diary = diaries[target_date]
                result_msg = f"【舔狗日记 - {diary['time']}】\n{diary['content']}"
                if 'emotion_score' in diary and diary['emotion_score'] > 0:
                    result_msg += f"\n(情感强度: {diary['emotion_score']}/10)"
                yield event.plain_result(result_msg)
            else:
                yield event.plain_result(f"未找到日期为 {target_date} 的日记记录。")
            return
        
        diary_list = []
        sorted_diaries = sorted(diaries.items(), key=lambda x: date.fromisoformat(x[0]), reverse=True)
        for diary_date, diary in sorted_diaries:
            important_mark = "⭐" if diary.get('important', False) else ""
            emotion_score = diary.get('emotion_score', 'N/A')
            diary_list.append(f"{diary_date} - {diary['time'].split(' ')[1]} {important_mark} (情感强度: {emotion_score}/10)")
        
        yield event.plain_result(f"【舔狗日记列表】\n" + "\n".join(diary_list))

    @filter.command("舔狗帮助")
    async def help_command(self, event: AstrMessageEvent):
        help_text = (
            "【舔狗日记插件帮助】\n"
            "这是一个生成幽默舔狗日记的插件，模拟人类记忆遗忘机制。\n\n"
            "可用指令列表：\n"
            "- 今日舔狗日记：查看或生成当天的舔狗日记。\n"
            "- 舔狗日记：基于历史记忆临时生成一份舔狗日记，不保存。\n"
            "- 舔狗日记列表：列出所有日记的日期和天气信息，可附加日期（如 '舔狗日记列表 4.17'）查看特定日期日记。\n"
            "- 重写舔狗日记：重写当天的舔狗日记，覆盖原有内容。\n"
            "- 舔狗帮助：显示本帮助信息。\n\n"
            f"日记将每天在 {self.auto_generate_time} 自动生成，"
            f"在 {self.auto_send_time} 自动发送到指定群组。"
        )
        yield event.plain_result(help_text)

    # @filter.command("测试发送舔狗日记")
    # async def test_send_diary(self, event: AstrMessageEvent):
    #     group_id = event.get_group_id()
    #     if not group_id:
    #         yield event.plain_result("此命令只能在群聊中使用，用于测试日记发送功能。")
    #         return
        
    #     today = date.today().isoformat()
    #     diaries = self._load_diaries()
    #     if today not in diaries:
    #         yield event.plain_result("今天尚未生成日记，无法测试发送。")
    #         return
        
    #     diary_content = diaries[today]['content']
    #     time_str = diaries[today]['time']
    #     emotion_score = diaries[today].get('emotion_score', 0)
    #     result_msg = f"【测试舔狗日记 - {time_str}】\n{diary_content}"
    #     if emotion_score > 0:
    #         result_msg += f"\n(情感强度: {emotion_score}/10)"
        
    #     # 检查是否需要以转发样式发送
    #     use_forward_style = len(result_msg) > FORWARD_THRESHOLD
    #     success_count = 0
    #     total_count = len(self.auto_send_groups) if self.auto_send_groups else 0
        
    #     # 首先给当前群聊发送反馈
    #     try:
    #         if use_forward_style and hasattr(event, 'bot'):
    #             forward_node = {
    #                 "type": "node",
    #                 "data": {
    #                     "name": "舔狗日记",
    #                     "uin": "123456789",
    #                     "content": [
    #                         {"type": "text", "data": {"text": result_msg}}
    #                     ]
    #                 }
    #             }
    #             await event.bot.send_group_msg(group_id=int(group_id), message=[forward_node])
    #         else:
    #             await event.bot.send_group_msg(group_id=int(group_id), message=result_msg)
    #         logger.info(f"测试发送日记到当前群组 {group_id} 成功")
    #     except Exception as e:
    #         yield event.plain_result(f"测试发送到当前群组失败：{str(e)}")
    #         logger.error(f"测试发送日记到当前群组 {group_id} 失败: {e}")
    #         return
        
    #     yield event.plain_result("正在测试发送日记到所有配置的定时群组...")
        
    #     if not total_count:
    #         yield event.plain_result("未配置任何定时发送群组，测试结束。")
    #         return
        
    #     # 然后遍历所有配置的定时群组进行测试发送
    #     for target_group_id in self.auto_send_groups:
    #         try:
    #             if use_forward_style and hasattr(event, 'bot'):
    #                 forward_node = {
    #                     "type": "node",
    #                     "data": {
    #                         "name": "舔狗日记",
    #                         "uin": "123456789",
    #                         "content": [
    #                             {"type": "text", "data": {"text": result_msg}}
    #                         ]
    #                     }
    #                 }
    #                 await event.bot.send_group_msg(group_id=int(target_group_id), message=[forward_node])
    #             else:
    #                 await event.bot.send_group_msg(group_id=int(target_group_id), message=result_msg)
    #             logger.info(f"测试发送日记到定时群组 {target_group_id} 成功")
    #             success_count += 1
    #             await asyncio.sleep(1)  # 防止发送过快被限制
    #         except ActionFailed as e:
    #             logger.error(f"测试发送日记到定时群组 {target_group_id} 失败 (ActionFailed): {e}")
    #         except Exception as e:
    #             logger.error(f"测试发送日记到定时群组 {target_group_id} 失败: {e}")
        
    #     yield event.plain_result(f"测试发送完成！成功发送到 {success_count}/{total_count} 个定时群组。")
    #     if success_count < total_count:
    #         yield event.plain_result("部分群组发送失败，请检查配置的群组ID是否正确或是否有权限。")

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
