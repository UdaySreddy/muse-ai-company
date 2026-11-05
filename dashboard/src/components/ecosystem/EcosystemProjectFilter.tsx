import { useQueryClient } from '@tanstack/react-query';
import { Check, ChevronsUpDown, Folder, FolderOpen } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Skeleton } from '@/components/ui/skeleton';
import { useProject } from '@/context/ProjectContext';
import { useProjects } from '@/api/projects';

/**
 * Ecosystem project filter — choose "which project's ecosystem library to view". Each project has ecosystem repo profiles for its own direction.
 *
 * ⚠️ Scope: this is an **ecosystem-only** project filter, used solely on the ecosystem list page (/ecosystem).
 *    Never move it back into the global Header / components/layout — historic lesson (v1.6.0): this component was originally named
 *    `ProjectSwitcher`, lived in components/layout, hung on the top-right Header, and was annotated in client.ts as
 *    "Global project context", which got it mistaken for a global app switcher; in reality, apart from ecosystem, no
 *    page actually consumes it (the task wall / reports etc. each have in-page filters). The name + directory have been corrected to lock in the semantics.
 *    If a global project switcher is ever really needed, build a new component; do not reuse this one.
 *
 * On switch:
 *   1. ProjectContext.switchProject synchronously updates api/client's module-level state
 *      (X-Project-Id header) — written before setState to avoid a race
 *   2. invalidateQueries — refreshes queries so the ecosystem list refetches for the new project
 *      (Note: X-Project-Id is currently attached to all requests, so switching also affects other header-scoped calls;
 *       this is a known legacy issue; it may later change to a per-call header attached only to ecosystem requests)
 */
export function EcosystemProjectFilter() {
  const { projectId, projectName, switchProject, clearProject } = useProject();
  const { data, isLoading } = useProjects();
  const qc = useQueryClient();

  const projects = data?.data ?? [];

  /**
   * The project list itself is not isolated by X-Project-Id; do not let it refetch on switch
   * (to avoid flicker). All other queries (ecosystem/tasks/projects/{id}/...) must refresh.
   */
  const invalidateProjectScopedQueries = () => {
    void qc.invalidateQueries({
      predicate: (q) => {
        const k = q.queryKey;
        if (Array.isArray(k) && k[0] === 'projects' && k.length === 1) return false;
        return true;
      },
    });
  };

  const handleSelect = (id: string, path: string, name: string) => {
    if (id === projectId) return;
    switchProject(id, path, name);
    invalidateProjectScopedQueries();
  };

  const handleClear = () => {
    if (projectId === null) return;
    clearProject();
    invalidateProjectScopedQueries();
  };

  if (isLoading) {
    return <Skeleton className="h-8 w-48" />;
  }

  const triggerLabel = projectName ?? 'All Projects';

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="Switch ecosystem project"
        className="inline-flex items-center gap-1.5 h-8 px-3 max-w-[260px] rounded-md border border-input bg-background text-sm font-medium shadow-xs hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring transition-colors"
      >
        {projectId ? (
          <FolderOpen className="h-3.5 w-3.5 text-primary shrink-0" aria-hidden="true" />
        ) : (
          <Folder className="h-3.5 w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
        )}
        <span className="truncate">{triggerLabel}</span>
        <ChevronsUpDown className="h-3 w-3 text-muted-foreground shrink-0" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <div className="px-2 py-1.5 text-xs font-medium text-muted-foreground">Switch Ecosystem Project</div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleClear}>
          <span className="flex items-center gap-2 flex-1">
            <Folder className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            <span className="text-sm">All Projects</span>
          </span>
          {projectId === null && <Check className="h-3.5 w-3.5" aria-hidden="true" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {projects.length === 0 ? (
          <div className="px-2 py-3 text-xs text-muted-foreground text-center">No projects yet</div>
        ) : (
          projects.map((p) => (
            <DropdownMenuItem
              key={p.id}
              onClick={() => handleSelect(p.id, p.root_path, p.name)}
            >
              <span className="flex items-center gap-2 flex-1 min-w-0">
                <FolderOpen
                  className={`h-3.5 w-3.5 shrink-0 ${p.id === projectId ? 'text-primary' : 'text-muted-foreground'}`}
                  aria-hidden="true"
                />
                <span className="flex flex-col min-w-0">
                  <span className="text-sm truncate">{p.name}</span>
                  {p.root_path && (
                    <span className="text-[10px] text-muted-foreground truncate">{p.root_path}</span>
                  )}
                </span>
              </span>
              {p.id === projectId && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
