import { Link } from 'react-router-dom';
import { Network, ArrowRight, ArrowLeft, ExternalLink } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RELATION_TYPE_LABELS } from '@/api/ecosystem';
import type { EcosystemRelation } from '@/api/ecosystem';

interface RelationsSectionProps {
  /** Current repo → other repos */
  outgoing: EcosystemRelation[];
  /** Other repos → current repo */
  incoming: EcosystemRelation[];
  /** The current repo's repo_full_name (used to mark the self node) */
  currentRepoFullName: string;
}

function relationLabel(rt: string): string {
  return RELATION_TYPE_LABELS[rt] ?? rt;
}

/** A single relation row */
function RelationRow({
  relation,
  direction,
  currentRepoFullName,
}: {
  relation: EcosystemRelation;
  direction: 'outgoing' | 'incoming';
  currentRepoFullName: string;
}) {
  const otherFullName =
    direction === 'outgoing'
      ? relation.target_repo_full_name
      : relation.source_repo_full_name;
  const otherId =
    direction === 'outgoing' ? relation.target_repo_id : relation.source_repo_id;

  const ArrowIcon = direction === 'outgoing' ? ArrowRight : ArrowLeft;
  const directionLabel = direction === 'outgoing' ? 'Points To' : 'From';

  return (
    <div className="flex items-center gap-2 py-1.5 text-xs border-b border-border/40 last:border-0">
      <Badge variant="outline" className="text-[10px] shrink-0">
        {relationLabel(relation.relation_type)}
      </Badge>
      <ArrowIcon className="h-3 w-3 text-muted-foreground shrink-0" aria-hidden="true" />
      <Link
        to={`/ecosystem/${otherId}`}
        className="font-medium text-primary hover:underline truncate min-w-0 flex-1"
        title={otherFullName ?? otherId}
      >
        {otherFullName ?? otherId}
      </Link>
      {otherFullName && (
        <a
          href={`https://github.com/${otherFullName}`}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${otherFullName} on GitHub`}
          className="text-muted-foreground hover:text-primary shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      )}
      <span className="text-[10px] text-muted-foreground shrink-0" title="Confidence">
        {(relation.confidence * 100).toFixed(0)}%
      </span>
      <span className="sr-only">
        {directionLabel} {currentRepoFullName}
      </span>
    </div>
  );
}

/**
 * Related repos section — shows citation / derivation relations between the current repo and other repos.
 */
export function RelationsSection({
  outgoing,
  incoming,
  currentRepoFullName,
}: RelationsSectionProps) {
  const total = (outgoing?.length ?? 0) + (incoming?.length ?? 0);

  if (total === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Network className="h-4 w-4" aria-hidden="true" />
            Related Repos
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No relations yet — the tagger/deep review has not identified any citation, derivation or replacement relations.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Network className="h-4 w-4" aria-hidden="true" />
          Related Repos ({total})
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {outgoing && outgoing.length > 0 && (
          <div>
            <h4 className="text-xs font-medium text-muted-foreground mb-1.5">
              Current Repo Points To ({outgoing.length})
            </h4>
            <div className="space-y-0">
              {outgoing.map((rel) => (
                <RelationRow
                  key={rel.id}
                  relation={rel}
                  direction="outgoing"
                  currentRepoFullName={currentRepoFullName}
                />
              ))}
            </div>
          </div>
        )}
        {incoming && incoming.length > 0 && (
          <div>
            <h4 className="text-xs font-medium text-muted-foreground mb-1.5">
              Cited By ({incoming.length})
            </h4>
            <div className="space-y-0">
              {incoming.map((rel) => (
                <RelationRow
                  key={rel.id}
                  relation={rel}
                  direction="incoming"
                  currentRepoFullName={currentRepoFullName}
                />
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
