/**
 * 「技能 → 企业」页签里系统管理员看到的管理视图：上传 ZIP、发布/停用、环境变量、发给哪些空间、
 * 从空间技能收编。开源的技能组件（ZipPicker、EnvDialog）直接复用；列表与详情抽屉自己搭，
 * 因为开源技能页没有「平台范围」的 scope 抽象（SkillScope 只有 workspace/personal，路径写死）。
 */
import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@openai/apps-sdk-ui/components/Button';
import { FileUpload } from '@openai/apps-sdk-ui/components/Icon';
import { readableError } from '@knowwork/web/api/http';
import { formatDateTime } from '@knowwork/web/utils/format';
import { Dialog } from '@knowwork/web/ui/Dialog';
import { toast } from '@knowwork/web/ui/toastStore';
import { Empty } from '@knowwork/web/ui/Empty';
import { Skeleton } from '@knowwork/web/ui/Skeleton';
import { StatusDot } from '@knowwork/web/ui/StatusDot';
import { Table, type Column } from '@knowwork/web/ui/Table';
import { Toolbar } from '@knowwork/web/features/admin/AdminPage';
import { RowActions } from '@knowwork/web/features/admin/common';
import { EnvDialog } from '@knowwork/web/features/admin/skills/EnvDialog';
import { ZipPicker, type ZipPickerHandle } from '@knowwork/web/features/admin/skills/ZipPicker';
import type { SkillManifest } from '@knowwork/web/api/skills';
import {
  deletePlatformSkill,
  getPlatformSkill,
  installPlatformSkill,
  listPlatformSkills,
  listPromotableSkills,
  promoteSkill,
  setPlatformAudience,
  publishPlatformSkill,
  setPlatformEnabled,
  setPlatformEnv,
  SKILL_KEYS,
  type PlatformSkill,
  type PromotableSkill,
  type SkillReleaseView,
} from '../api';
import { AudienceEditor, audienceText, useSetAudience, useWorkspaces } from '../audience';

/** 技能状态（同开源 skillsModel 的 skillState，但那只认空间技能的字段，这里给平台版精简的）。 */
function stateOf(skill: PlatformSkill): { status: 'success' | 'warning' | 'default'; text: string } {
  if (!skill.enabled) return { status: 'default', text: '已停用' };
  if (skill.status === 'published') {
    return skill.hasUnpublishedChanges
      ? { status: 'warning', text: '已发布 · 有新草稿' }
      : { status: 'success', text: '已发布' };
  }
  return { status: 'warning', text: '草稿' };
}

function DetailDrawer({
  skillId,
  onClose,
  onEditEnv,
}: {
  readonly skillId: string;
  readonly onClose: () => void;
  readonly onEditEnv: (skill: PlatformSkill) => void;
}) {
  const detail = useQuery({ queryKey: ['admin', 'platform-skills', skillId], queryFn: () => getPlatformSkill(skillId) });
  const manifest: SkillManifest | null = detail.data?.skill.published ?? detail.data?.skill.draft ?? null;
  return (
    <Dialog title={detail.data?.skill.name ?? '企业技能'} onClose={onClose} size="md" footer={null}>
      {detail.isPending ? (
        <Skeleton rows={6} />
      ) : detail.isError ? (
        <p className="kw-secondary-text">{readableError(detail.error, '详情没有加载出来')}</p>
      ) : (
        <>
          <p className="kw-secondary-text">{detail.data.skill.description || '（没有说明）'}</p>
          <div className="kw-labeled">
            <span className="kw-secondary-text">环境变量</span>
            {(manifest?.env ?? []).length === 0 ? (
              <span>没有声明</span>
            ) : (
              <Button color="secondary" variant="ghost" onClick={() => onEditEnv(detail.data.skill)}>
                填写
              </Button>
            )}
          </div>
          <h4 className="kw-heading">版本历史</h4>
          {(detail.data.releases.length === 0 ? (
            <p className="kw-secondary-text">还没有发布过。</p>
          ) : (
            <ul className="kw-list">
              {detail.data.releases.map((release: SkillReleaseView) => (
                <li key={release.id} className="kw-labeled">
                  <span className="kw-num">{release.version}</span>
                  <span className="kw-secondary-text">
                    {release.releasedAt ? formatDateTime(release.releasedAt) : ''} · {release.releasedBy}
                  </span>
                </li>
              ))}
            </ul>
          ))}
        </>
      )}
    </Dialog>
  );
}

function PromoteDialog({ onClose }: { readonly onClose: () => void }) {
  const client = useQueryClient();
  const sources = useQuery({ queryKey: ['admin', 'platform-skills-promotable'], queryFn: listPromotableSkills });
  const promote = useMutation({
    mutationFn: (item: PromotableSkill) => promoteSkill(item.workspaceId, item.skillId),
    onSuccess: async (skill) => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
      toast.success(`已收为企业技能「${skill.name}」（第一次是新建，再收是它的新草稿）`);
      onClose();
    },
    onError: (cause) => toast.error(readableError(cause, '没有收编成功')),
  });
  return (
    <Dialog title="从空间技能收编" onClose={onClose} size="md" footer={null}>
      {sources.isPending ? (
        <Skeleton rows={4} />
      ) : sources.isError ? (
        <p className="kw-secondary-text">{readableError(sources.error, '空间技能列表没有加载出来')}</p>
      ) : (sources.data ?? []).length === 0 ? (
        <p className="kw-secondary-text">各空间还没有已发布的空间技能。</p>
      ) : (
        (sources.data ?? []).map((item) => (
          <button
            key={`${item.workspaceId}/${item.skillId}`}
            type="button"
            className="kw-list-item"
            disabled={promote.isPending}
            onClick={() => promote.mutate(item)}
          >
            <span>{item.name}</span>
            <span className="kw-secondary-text">{`${item.workspaceName} · ${item.version || '未带版本'}`}</span>
          </button>
        ))
      )}
    </Dialog>
  );
}

export function SkillsEnterprisePanel() {
  const workspaces = useWorkspaces();
  const skills = useQuery({ queryKey: [...SKILL_KEYS.list], queryFn: listPlatformSkills });
  const client = useQueryClient();
  const invalidate = [[...SKILL_KEYS.list]] as const;
  const pickerRef = useRef<ZipPickerHandle>(null);
  const [detailId, setDetailId] = useState<string | undefined>();
  const [envEditing, setEnvEditing] = useState<PlatformSkill | null>(null);
  const [audienceEditing, setAudienceEditing] = useState<PlatformSkill | null>(null);
  const [promoting, setPromoting] = useState(false);

  const install = useMutation({
    mutationFn: (file: File) => installPlatformSkill(file),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
      toast.success('已上传为草稿，发布后成员才能用');
    },
    onError: (cause) => toast.error(readableError(cause, '上传没有成功')),
  });
  const publish = useMutation({
    mutationFn: (skill: PlatformSkill) => publishPlatformSkill(skill.id, skill.draftRevision),
    onSuccess: async (release) => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
      toast.success(`已发布${release?.version ? ` v${release.version}` : ''}`);
    },
    onError: (cause) => toast.error(readableError(cause, '发布没有成功')),
  });
  const toggle = useMutation({
    mutationFn: ({ skill, enabled }: { skill: PlatformSkill; enabled: boolean }) => setPlatformEnabled(skill.id, enabled),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
    },
    onError: (cause) => toast.error(readableError(cause, '没有切换成功')),
  });
  const remove = useMutation({
    mutationFn: (skill: PlatformSkill) => deletePlatformSkill(skill.id),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
      toast.success('已删除企业技能');
    },
    onError: (cause) => toast.error(readableError(cause, '没有删除成功')),
  });
  const saveEnv = useMutation({
    mutationFn: ({ skill, values }: { skill: PlatformSkill; values: Record<string, string | null> }) =>
      setPlatformEnv(skill.id, values),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: [...SKILL_KEYS.list] });
    },
  });
  const setAudience = useSetAudience(setPlatformAudience, invalidate);

  const columns: readonly Column<PlatformSkill>[] = [
      { key: 'name', title: '名称', width: 200, ellipsis: true, render: (skill) => skill.name },
      { key: 'description', title: '说明', ellipsis: true, render: (skill) => skill.description },
      {
        key: 'state',
        title: '状态',
        width: 170,
        render: (skill) => {
          const state = stateOf(skill);
          return <StatusDot tone={state.status}>{state.text}</StatusDot>;
        },
      },
      {
        key: 'audience',
        title: '发给哪些空间',
        width: 200,
        ellipsis: true,
        render: (skill) => audienceText(skill.audience ?? [], workspaces.data),
      },
      {
        key: 'version',
        title: '发布版本',
        width: 110,
        render: (skill) => <span className="kw-num">{skill.publishedVersion ?? '-'}</span>,
      },
      {
        key: 'updated',
        title: '更新时间',
        width: 170,
        render: (skill) => (skill.updatedAt ? formatDateTime(skill.updatedAt) : '-'),
      },
      {
        key: 'actions',
        title: '操作',
        width: 220,
        render: (skill) => (
          <RowActions
            actions={[
              { key: 'audience', label: '发放', onClick: () => setAudienceEditing(skill) },
              { key: 'detail', label: '详情', onClick: () => setDetailId(skill.id) },
              ...(skill.status === 'draft' || skill.hasUnpublishedChanges
                ? [{ key: 'publish', label: '发布', onClick: () => publish.mutate(skill) }]
                : []),
              ...(manifestEnv(skill).length > 0
                ? [{ key: 'env', label: '环境变量', onClick: () => setEnvEditing(skill) }]
                : []),
              {
                key: 'toggle',
                label: skill.enabled ? '停用' : '启用',
                onClick: () => toggle.mutate({ skill, enabled: !skill.enabled }),
              },
              {
                key: 'delete',
                label: '删除',
                danger: true,
                confirm: `删除企业技能「${skill.name}」？发到的各空间将不再提供它。`,
                onClick: () => remove.mutate(skill),
              },
            ]}
          />
        ),
      },
  ];

  const rows = skills.data ?? [];
  return (
    <>
      <Toolbar
        right={
          <>
            <Button color="secondary" onClick={() => setPromoting(true)}>从空间技能收编…</Button>
            <Button color="primary" onClick={() => pickerRef.current?.open()}>
              <FileUpload />
              上传技能包
            </Button>
          </>
        }
      />
      {skills.isPending ? (
        <Skeleton rows={5} />
      ) : skills.isError ? (
        <Empty title={readableError(skills.error, '企业技能没有加载出来')} />
      ) : rows.length === 0 ? (
        <Empty
          title="还没有企业技能"
          description="上传 SKILL.md 打包的 ZIP，或把空间里已发布的技能收编进来；发布后发给各空间。"
        />
      ) : (
        <Table<PlatformSkill> rowKey={(skill) => skill.id} columns={columns} rows={rows} minWidth={1080} loading={skills.isFetching} />
      )}
      <ZipPicker ref={pickerRef} onPick={(file) => install.mutate(file)} />
      {detailId ? (
        <DetailDrawer skillId={detailId} onClose={() => setDetailId(undefined)} onEditEnv={setEnvEditing} />
      ) : null}
      {envEditing ? (
        <EnvDialog
          name={envEditing.name}
          env={manifestEnv(envEditing)}
          filled={envEditing.envFilled}
          pending={saveEnv.isPending}
          onSave={async (values) => {
            try {
              // 失效查询在 saveEnv 的 onSuccess 里做一次，这里不再重复
              await saveEnv.mutateAsync({ skill: envEditing, values });
              setEnvEditing(null);
              return true;
            } catch (error) {
              toast.error(readableError(error, '没有保存成功'));
              return false;
            }
          }}
          onClose={() => setEnvEditing(null)}
        />
      ) : null}
      {audienceEditing ? (
        <AudienceEditor
          title={`「${audienceEditing.name}」发给哪些空间`}
          audience={audienceEditing.audience ?? []}
          pending={setAudience.pending}
          onSave={(list) => setAudience.run({ id: audienceEditing.id, workspaces: list })}
          onClose={() => setAudienceEditing(null)}
        />
      ) : null}
      {promoting ? <PromoteDialog onClose={() => setPromoting(false)} /> : null}
    </>
  );
}

/** 技能声明了哪些环境变量：看发布版本，没发布过看草稿。 */
function manifestEnv(skill: PlatformSkill) {
  return (skill.published ?? skill.draft)?.env ?? [];
}
