import { useCallback } from 'react';
import useWebSocket from 'react-use-websocket';
import { useQueryClient } from '@tanstack/react-query';
import { WS_URL } from '../api/client';
import { useWSStore } from '../stores/websocket';
import type { WSEvent } from '../types';

export function useRealtimeEvents() {
  const queryClient = useQueryClient();
  const { setConnected, addEvent } = useWSStore();

  const onMessage = useCallback(
    (event: MessageEvent) => {
      try {
        const raw = JSON.parse(event.data as string);

        // Skip non-event messages (e.g. ack)
        if (raw.type !== 'event') return;

        // Use event_type as the actual type
        const data: WSEvent = {
          type: raw.event_type ?? raw.type,
          source: raw.channel ?? '',
          data: raw.data ?? {},
          timestamp: raw.timestamp ?? '',
        };
        addEvent(data);

        // Invalidate relevant queries based on event type
        // Note: invalidateQueries matches by prefix, so ['teams'] matches
        // ['teams'], ['teams', id], ['teams', id, 'agents'], etc.
        const t = data.type;
        if (t.startsWith('team')) {
          void queryClient.invalidateQueries({ queryKey: ['teams'] });
        }
        if (t.startsWith('task')) {
          // useTasks uses ['teams', teamId, 'tasks'] — covered by ['teams'] invalidation
          // but also need standalone task queries and task-wall
          void queryClient.invalidateQueries({ queryKey: ['teams'] });
          void queryClient.invalidateQueries({ queryKey: ['tasks'] });
          void queryClient.invalidateQueries({ queryKey: ['task-wall'] });
          void queryClient.invalidateQueries({ queryKey: ['project-task-wall'] });
        }
        if (t.startsWith('agent')) {
          // useAgents uses ['teams', teamId, 'agents'] — covered by ['teams']
          void queryClient.invalidateQueries({ queryKey: ['teams'] });
          void queryClient.invalidateQueries({ queryKey: ['activities'] });
        }
        if (t.startsWith('meeting')) {
          void queryClient.invalidateQueries({ queryKey: ['meetings'] });
        }
        if (t.startsWith('workflow')) {
          // workflow.planned/started/completed -> invalidate run list and detail (prefix match)
          void queryClient.invalidateQueries({ queryKey: ['workflows'] });
        }
        if (t.startsWith('project')) {
          void queryClient.invalidateQueries({ queryKey: ['projects'] });
          void queryClient.invalidateQueries({ queryKey: ['project-task-wall'] });
        }
        if (t.startsWith('cc.')) {
          // CC hook events: refresh agents and teams (may include auto-created agents)
          void queryClient.invalidateQueries({ queryKey: ['teams'] });
          void queryClient.invalidateQueries({ queryKey: ['activities'] });
        }
        // All events should refresh the event list
        void queryClient.invalidateQueries({ queryKey: ['events'] });
      } catch {
        // ignore malformed messages
      }
    },
    [queryClient, addEvent],
  );

  const { readyState, sendJsonMessage } = useWebSocket(WS_URL, {
    onOpen: () => {
      setConnected(true);
      sendJsonMessage({ type: 'subscribe', channel: '*' });
    },
    onClose: () => setConnected(false),
    onError: () => setConnected(false),
    onMessage,
    shouldReconnect: () => true,
    reconnectAttempts: 10,
    reconnectInterval: 3000,
  });

  return { readyState };
}
