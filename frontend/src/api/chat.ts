import { apiClient, getAuthToken } from './client';
import { ChatMessage, Conversation, RetrievalTestResponse } from '../types';

export interface SSEChatEvent {
  event: 'start' | 'route' | 'query_rewrite' | 'retrieval' | 'context_budget' | 'token' | 'citations' | 'trace' | 'done' | 'error';
  data: any;
}

export interface SSEAgentRunEvent {
  event:
    | 'run_started'
    | 'agent_step_completed'
    | 'tool_call_started'
    | 'tool_call_completed'
    | 'citation_verified'
    | 'final_answer'
    | 'trace'
    | 'run_completed'
    | 'run_failed'
    | 'run_cancelled';
  data: any;
}

export const sendAgentRunStream = async (
  agentId: string = 'default',
  data: {
    input: string;
    conversation_id?: string;
    knowledge_base_id?: string;
    model_id?: string;
  },
  onEvent: (event: SSEAgentRunEvent) => void,
  signal?: AbortSignal
): Promise<void> => {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const token = getAuthToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(`/api/v1/agents/${agentId}/runs/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(data),
    signal,
  });

  if (!response.ok) {
    let errorDetail = `HTTP ${response.status}: ${response.statusText}`;
    try {
      const errJson = await response.json();
      if (errJson.detail) {
        errorDetail = errJson.detail;
      }
    } catch {
      // Ignore JSON parse failure on non-JSON error
    }
    throw new Error(errorDetail);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('ReadableStream not supported by browser');
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const parseMessageBlock = (block: string) => {
    let eventType = 'message';
    let dataStr = '';
    const lines = block.split(/\r?\n/);
    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventType = line.replace(/^event:\s*/, '').trim();
      } else if (line.startsWith('data:')) {
        const d = line.replace(/^data:\s*/, '');
        dataStr = dataStr ? `${dataStr}\n${d}` : d;
      }
    }
    if (dataStr) {
      try {
        const parsed = JSON.parse(dataStr);
        onEvent({ event: eventType as any, data: parsed });
      } catch {
        onEvent({ event: eventType as any, data: dataStr });
      }
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (value) {
        buffer += decoder.decode(value, { stream: !done });
      }

      let match: RegExpMatchArray | null;
      while ((match = buffer.match(/\r?\n\r?\n/))) {
        const boundaryIndex = match.index!;
        const matchLength = match[0].length;
        const block = buffer.slice(0, boundaryIndex).trim();
        buffer = buffer.slice(boundaryIndex + matchLength);
        if (block) {
          parseMessageBlock(block);
        }
      }

      if (done) {
        buffer += decoder.decode();
        const remaining = buffer.trim();
        if (remaining) {
          parseMessageBlock(remaining);
        }
        break;
      }
    }
  } catch (err: any) {
    if (err.name === 'AbortError') {
      return;
    }
    throw err;
  }
};

export const sendChatMessageStream = async (
  data: {
    knowledge_base_id?: string;
    query: string;
    conversation_id?: string;
    mode?: string;
    top_k?: number;
    temperature?: number;
    model_id?: string;
  },
  onEvent: (event: SSEChatEvent) => void,
  signal?: AbortSignal
): Promise<void> => {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const token = getAuthToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch('/api/v1/chat/completions/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify(data),
    signal,
  });

  if (!response.ok) {
    let errorDetail = `HTTP ${response.status}: ${response.statusText}`;
    try {
      const errJson = await response.json();
      if (errJson.detail) {
        errorDetail = errJson.detail;
      }
    } catch {
      // Ignore JSON parse failure on non-JSON error
    }
    throw new Error(errorDetail);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('ReadableStream not supported by browser');
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const parseMessageBlock = (block: string) => {
    let eventType = 'message';
    let dataStr = '';
    const lines = block.split(/\r?\n/);
    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventType = line.replace(/^event:\s*/, '').trim();
      } else if (line.startsWith('data:')) {
        const d = line.replace(/^data:\s*/, '');
        dataStr = dataStr ? `${dataStr}\n${d}` : d;
      }
    }
    if (dataStr) {
      try {
        const parsed = JSON.parse(dataStr);
        onEvent({ event: eventType as any, data: parsed });
      } catch {
        onEvent({ event: eventType as any, data: dataStr });
      }
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (value) {
        buffer += decoder.decode(value, { stream: !done });
      }

      let match: RegExpMatchArray | null;
      while ((match = buffer.match(/\r?\n\r?\n/))) {
        const boundaryIndex = match.index!;
        const matchLength = match[0].length;
        const block = buffer.slice(0, boundaryIndex).trim();
        buffer = buffer.slice(boundaryIndex + matchLength);
        if (block) {
          parseMessageBlock(block);
        }
      }

      if (done) {
        buffer += decoder.decode(); // flush remaining
        const remaining = buffer.trim();
        if (remaining) {
          parseMessageBlock(remaining);
        }
        break;
      }
    }
  } catch (err: any) {
    if (err.name === 'AbortError') {
      // Request intentionally aborted by user
      return;
    }
    throw err;
  }
};

export const sendChatMessage = async (data: {
  knowledge_base_id: string;
  query: string;
  conversation_id?: string;
  mode?: string;
  top_k?: number;
}): Promise<{
  answer: string;
  citations: any[];
  execution_info: any;
  conversation_id: string;
  message_id: string;
}> => {
  const res = await apiClient.post('/chat/completions', data);
  return res.data;
};

export const createConversation = async (data: {
  knowledge_base_id?: string;
  title?: string;
  mode?: string;
}): Promise<Conversation> => {
  const res = await apiClient.post<Conversation>('/conversations', data);
  return res.data;
};

export const getConversations = async (knowledgeBaseId?: string): Promise<Conversation[]> => {
  const params = knowledgeBaseId ? { knowledge_base_id: knowledgeBaseId } : {};
  const res = await apiClient.get<Conversation[]>('/conversations', { params });
  return res.data;
};

export const getConversationMessages = async (convId: string): Promise<ChatMessage[]> => {
  const res = await apiClient.get<ChatMessage[]>(`/conversations/${convId}/messages`);
  return res.data;
};

export const deleteConversation = async (convId: string): Promise<void> => {
  await apiClient.delete(`/conversations/${convId}`);
};

export const testRetrieval = async (
  kbId: string,
  query: string,
  topK: number = 5,
  mode: 'rerank' | 'hybrid' | 'dense' | 'bm25' = 'rerank'
): Promise<RetrievalTestResponse> => {
  const endpoint =
    mode === 'bm25'
      ? '/retrieval/bm25-test'
      : mode === 'dense'
      ? '/retrieval/test'
      : mode === 'hybrid'
      ? '/retrieval/hybrid-test'
      : '/retrieval/rerank-test';

  const res = await apiClient.post<RetrievalTestResponse>(endpoint, {
    knowledge_base_id: kbId,
    query,
    top_k: topK,
  });
  return res.data;
};

export const getHealth = async (): Promise<any> => {
  const res = await apiClient.get('/health');
  return res.data;
};
