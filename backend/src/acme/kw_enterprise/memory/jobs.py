"""记忆层的任务类型：api（入队）与 worker（执行）共用的唯一定义，两边不会各配一套
（照 jobs/kinds.py 的纪律）。

池的选择（联调修正）：扩展**无法自建池**——worker 的执行槽数来自上游 JobSettings 的固定
白名单（core/enrichment/maintenance），JobRunner.pools 只在白名单内按注册的任务类型生效，
新池名永远没有执行槽。所以：
- EXTRACT 走 core（每轮一次的轻量 LLM 调用，低延迟诉求，core 的 2 槽够用）；
- ORGANIZE 走 maintenance（每人一天至多一条的批处理味任务，与同步/导出同池，1 槽不互抢）。
"""

from knowwork_server.jobs import JobKind
from knowwork_server.jobs.kinds import SERVICE_DELAYS

# 整理：合并说法重复的、很久没提的降权、到期归档、腾位、补向量（设计 §7）。
# 合并键是 workspace+kind+subject：同一空间同一人一天至多排一条；多空间的用户可能各排一条，
# M3 的 handler 按 payload.user_id 整理全量个人记忆（数据不按空间筛），先到的做掉后到的就无活可干
ORGANIZE = JobKind("memory.organize", pool="maintenance", max_tries=3, lease_seconds=120.0,
                   timeout_seconds=600.0, retry_delays=SERVICE_DELAYS, version=1, cancelable=True,
                   label="记忆整理")

# 提取：回合结束入队，worker 里用结构化输出抽取候选再判重。subject=conversation_id，
# 同一会话连续几轮合并成一条、载荷换最新
EXTRACT = JobKind("memory.extract", pool="core", max_tries=3, lease_seconds=120.0, timeout_seconds=300.0,
                  retry_delays=SERVICE_DELAYS, version=1, cancelable=True, label="记忆提取")
