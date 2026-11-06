/**
 * 扩展前端入口：web 构建时经 KNOWWORK_WEB_EXTENSIONS 指到这里（vite.config.ts 的别名
 * @knowwork/extensions），install(register) 把菜单、页面与面板注册进命名插槽。
 * 插槽类型来自开源的 slots/registry.ts；只 import 类型，运行时不反向依赖注册方。
 */
import type { RouteObject } from 'react-router';
import { BookOpen } from '@openai/apps-sdk-ui/components/Icon';
import type { Register } from '@knowwork/web/slots/registry';
import { MemoryTab } from './pages/memory';
import { WorkspacesPage } from './pages/workspaces';
import { SkillsEnterprisePanel } from './panels/skills-enterprise';
import { ToolsEnterprisePanel } from './panels/tools-enterprise';

export function install(register: Register): void {
  // 管理后台「系统」分组的菜单项（只有系统管理员可见，排在「模型」后面）
  register('system.nav', { to: '/admin/workspaces', label: '空间管理', icon: <BookOpen /> });
  // 管理后台页面，path 相对 /admin
  register('admin.routes', { path: 'workspaces', element: <WorkspacesPage /> } satisfies RouteObject);
  // 「技能 → 企业」页签：系统管理员的管理面板（非系统管理员仍看本空间的设置，开源已分支）
  register('admin.skills.enterprise', { children: <SkillsEnterprisePanel /> });
  // 「工具与 MCP → 企业」页签：同上
  register('admin.tools.enterprise', { children: <ToolsEnterprisePanel /> });
  // 个人设置 → 我的记忆：对话里「管理记忆」的深链是 /settings?tab=memory（chat.tools 的 notice 用）
  register('settings.tabs', { key: 'memory', label: '我的记忆', children: <MemoryTab /> });
  // 「记住」工具在执行过程里的显示名与完成提示（回答下方一行，直达设置页）
  register('chat.tools', {
    name: 'remember',
    label: '记住',
    target: 'content',
    notice: (call) => {
      const content = typeof call.arguments.content === 'string' ? call.arguments.content : '';
      if (!content || !call.result.startsWith('已记住')) return null;
      return { text: `已记住：${content}`, link: { label: '管理记忆', to: '/settings?tab=memory' } };
    },
  });
}
