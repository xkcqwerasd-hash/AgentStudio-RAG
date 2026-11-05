export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embedding_model: string;
  chunk_size: number;
  chunk_overlap: number;
  document_count: number;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

export interface DocumentItem {
  id: string;
  knowledge_base_id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  status: 'PENDING' | 'PARSING' | 'CHUNKING' | 'EMBEDDING' | 'COMPLETED' | 'FAILED';
  page_count: number;
  chunk_count: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentChunk {
  id: string;
  document_id: string;
  knowledge_base_id: string;
  chunk_index: number;
  content: string;
  page_number: number;
  char_length: number;
  created_at: string;
}

export interface Citation {
  citation_index: number;
  chunk_id: string;
  document_id: string;
  document_name: string;
  page_number: number;
  similarity_score: number;
  content_snippet: string;
  source_type?: string;
  heading_path?: string[] | string;
  block_index?: number;
  line_start?: number;
  line_end?: number;
  provenance_label?: string;
}

export interface ConversationAttachment {
  id: string;
  conversation_id: string;
  original_filename: string;
  storage_key: string;
  file_extension: string;
  mime_type: string;
  file_size: number;
  parse_status: 'PENDING' | 'PARSING' | 'COMPLETED' | 'FAILED';
  page_count: number;
  char_length: number;
  token_count: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AttachmentPageItem {
  page_number: number;
  text: string;
  metadata: Record<string, any>;
}

export interface ConversationAttachmentDetail extends ConversationAttachment {
  parsed_pages: AttachmentPageItem[];
  extracted_text: string;
}

export interface QueryRewriteData {
  original_query: string;
  rewritten_query: string;
  is_rewritten: boolean;
  duration_ms: number;
  tokens_used?: number | null;
  fallback?: boolean;
  fallback_reason?: string | null;
}

export interface ContextBudgetInfo {
  model_context_window: number;
  input_budget: number;
  estimated_input_tokens: number;
  actual_input_tokens?: number | null;
  actual_output_tokens?: number | null;
  reserved_output_tokens: number;
  safety_margin: number;
  system_prompt_tokens: number;
  history_tokens: number;
  retrieval_tokens: number;
  query_tokens: number;
  history_messages_kept: number;
  history_messages_pruned: number;
  retrieval_chunks_kept: number;
  retrieval_chunks_pruned: number;
  budget_estimation_method: string;
}

export interface ExecutionInfo {
  mode: string;
  intent?: string | null;
  execution_path?: string | null;
  route_reason?: string | null;
  retrieved_chunks_count: number;
  retrieval_duration_ms: number;
  generation_duration_ms: number;
  total_duration_ms: number;
  llm_model: string;
  ttft_ms?: number | null;
  original_query?: string | null;
  rewritten_query?: string | null;
  is_rewritten?: boolean;
  rewrite_duration_ms?: number | null;
  rewrite_tokens_used?: number | null;
  // M2.4 Context Budget
  model_context_window?: number | null;
  estimated_input_tokens?: number | null;
  actual_input_tokens?: number | null;
  actual_output_tokens?: number | null;
  reserved_output_tokens?: number | null;
  history_tokens?: number | null;
  retrieval_tokens?: number | null;
  history_messages_kept?: number | null;
  history_messages_pruned?: number | null;
  retrieval_chunks_kept?: number | null;
  retrieval_chunks_pruned?: number | null;
  budget_estimation_method?: string | null;
  // M3.2 RRF Hybrid Retrieval
  retrieval_mode?: string | null;
  dense_duration_ms?: number | null;
  bm25_duration_ms?: number | null;
  rrf_duration_ms?: number | null;
  dense_chunks_count?: number | null;
  bm25_chunks_count?: number | null;
  deduplicated_chunks_count?: number | null;
  degradation_status?: string | null;
  // M3.3 Cross-Encoder Reranker
  reranker_enabled?: boolean;
  reranker_model?: string | null;
  reranker_duration_ms?: number | null;
  reranker_candidates_in?: number | null;
  reranker_candidates_out?: number | null;
  reranker_degraded?: boolean;
  reranker_degradation_reason?: string | null;
  // P0.3 Ephemeral Attachments
  attachment_count?: number;
  attachment_strategy?: string | null;
  attachment_duration_ms?: number | null;
  attachment_tokens?: number | null;
  attachment_chunks_kept?: number | null;
  attachment_chunks_pruned?: number | null;
  // P1.2 Long-term Memory
  memory_used?: boolean;
  retrieved_memory_count?: number;
  injected_memory_count?: number;
  memory_tokens?: number;
  memory_retrieval_duration_ms?: number | null;
}

export interface AgentToolCallItem {
  tool_call_id: string;
  tool_name: string;
  args?: Record<string, any>;
  status: 'calling' | 'success' | 'failed';
  duration_ms?: number;
  evidence_count?: number;
  observation_preview?: string;
  error?: string;
  source_type?: string;
  attachment_id?: string;
  original_filename?: string;
  retrieval_mode?: string;
  retrieved_chunks?: number;
  returned_chunks?: number;
  evidence_refs?: number[];
  evidence_tokens?: number;
}

export interface AgentStepItem {
  step: number;
  decision?: string;
  tools?: string[];
  tool_calls_count?: number;
  timestamp?: number;
}

export interface AgentExecutionTimeline {
  run_id?: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'max_steps_reached';
  current_step?: number;
  steps: AgentStepItem[];
  tool_calls: AgentToolCallItem[];
  citations_count?: number;
  duration_ms?: number;
  error?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  execution_info?: ExecutionInfo & { agent_timeline?: AgentExecutionTimeline };
  agent_timeline?: AgentExecutionTimeline;
  created_at: string;
}

export interface Conversation {
  id: string;
  knowledge_base_id: string | null;
  title: string;
  mode: string;
  message_count?: number;
  last_message_preview?: string | null;
  created_at: string;
  updated_at: string;
}

export interface RetrievalTestItem {
  chunk_id: string;
  document_id: string;
  document_name: string;
  page_number: number;
  similarity_score: number;
  content: string;
  dense_rank?: number | null;
  bm25_rank?: number | null;
  dense_score?: number | null;
  bm25_score?: number | null;
  fusion_score?: number | null;
  reranker_score?: number | null;
  reranker_rank?: number | null;
  reranker_raw_score?: number | null;
}

export interface RetrievalTestResponse {
  query: string;
  knowledge_base_id: string;
  total_retrieved: number;
  results: RetrievalTestItem[];
  duration_ms: number;
  dense_duration_ms?: number | null;
  bm25_duration_ms?: number | null;
  rrf_duration_ms?: number | null;
  reranker_duration_ms?: number | null;
  reranker_candidates_in?: number | null;
  reranker_candidates_out?: number | null;
  reranker_model?: string | null;
  reranker_degraded?: boolean;
  degradation_status?: string | null;
}

export interface ModelCapabilities {
  supports_streaming: boolean;
  supports_tool_calling: boolean;
  supports_vision: boolean;
  context_window: number;
}

export interface ModelConfig {
  id: string;
  provider_id: string;
  model_name: string;
  display_name: string;
  model_type: string;
  capabilities: ModelCapabilities;
  is_default: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelProvider {
  id: string;
  name: string;
  provider_type: string;
  base_url: string;
  has_api_key: boolean;
  masked_api_key?: string | null;
  enabled: boolean;
  models: ModelConfig[];
  created_at: string;
  updated_at: string;
}

export interface ActiveModelItem {
  id: string;
  provider_id: string;
  provider_name: string;
  provider_type: string;
  model_name: string;
  display_name: string;
  is_default: boolean;
  capabilities: ModelCapabilities;
}

export interface ModelTestResponse {
  success: boolean;
  latency_ms: number;
  reply_snippet?: string | null;
  error?: string | null;
}

export interface OllamaModelItem {
  name: string;
  size?: number | null;
  modified_at?: string | null;
  family?: string | null;
  parameter_size?: string | null;
  quantization_level?: string | null;
}

export interface OllamaProbeResponse {
  success: boolean;
  models: OllamaModelItem[];
  error?: string | null;
}

// ==============================================================================
// Long-term Memory Types (P1.2 Stage 4)
// ==============================================================================

export type MemoryType = 'USER_PROFILE' | 'PROJECT_CONTEXT' | 'IMPORTANT_FACT' | 'DIRECT_RULE';
export type MemorySourceType = 'EXPLICIT_USER' | 'AUTO_EXTRACTED';

export interface MemoryItem {
  id: string;
  owner_id: string;
  content: string;
  memory_type: MemoryType | string;
  tags: string[];
  source_type: MemorySourceType | string;
  source_conversation_id?: string | null;
  source_message_id?: string | null;
  confidence: number;
  access_count: number;
  last_accessed_at?: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface MemoryStats {
  total_memories: number;
  active_memories: number;
  profile_count: number;
  background_count: number;
  fact_count: number;
  rule_count: number;
  auto_memory_enabled: boolean;
}

export interface UserMemorySetting {
  owner_id: string;
  auto_memory_enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

// ==============================================================================
// User Identity & Authentication Types (P1.3 Stage 4)
// ==============================================================================

export type UserRole = 'admin' | 'user';

export interface UserProfile {
  id: string;
  username: string;
  email?: string | null;
  role: UserRole | string;
  is_active: boolean;
  created_at: string;
  last_login_at?: string | null;
}

export interface AuthModeInfo {
  deployment_mode: 'local' | 'server';
  allow_registration: boolean;
  version: string;
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export interface RegisterData {
  username: string;
  password: string;
  email?: string;
}

export interface PasswordChangeData {
  old_password: string;
  new_password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: UserProfile;
}

export interface LogoutResponse {
  message: string;
  revocation_type: string;
}
