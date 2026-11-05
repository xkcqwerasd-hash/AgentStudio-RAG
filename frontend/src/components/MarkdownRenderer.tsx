import React, { useState, useMemo } from 'react';
import { Tooltip, message } from 'antd';
import {
  CopyOutlined,
  CheckOutlined,
  FileTextOutlined,
  CodeOutlined,
  LinkOutlined,
} from '@ant-design/icons';
import { Citation } from '../types';
import { getProvenanceDisplay } from './CitationDrawer';

export interface MarkdownRendererProps {
  content: string;
  citations?: Citation[];
  onCitationClick?: (citationIndex: number) => void;
  isStreaming?: boolean;
  className?: string;
  style?: React.CSSProperties;
}

// ==========================================
// 1. Safe Clipboard Copy Helper
// ==========================================
const copyToClipboard = async (text: string): Promise<boolean> => {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fallback below
  }
  try {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    const success = document.execCommand('copy');
    textArea.remove();
    return success;
  } catch {
    return false;
  }
};

// ==========================================
// 2. Syntax Highlighting Helpers
// ==========================================
interface Token {
  type: 'keyword' | 'string' | 'comment' | 'number' | 'boolean' | 'fn' | 'text';
  text: string;
}

const TOKEN_COLORS: Record<Token['type'], string> = {
  keyword: '#93c5fd', // Light blue
  string: '#86efac',  // Emerald green
  comment: '#64748b', // Slate muted
  number: '#fdba74',  // Light orange
  boolean: '#f472b6', // Pink
  fn: '#67e8f9',      // Cyan
  text: '#e2e8f0',    // Off-white / light slate
};

const tokenizeCodeLine = (line: string, lang: string): Token[] => {
  const normLang = (lang || '').toLowerCase().trim();

  // Pick keyword pattern
  const pyKeywords =
    '\\b(def|class|import|from|as|return|if|elif|else|for|while|try|except|finally|with|async|await|pass|raise|yield|break|continue|lambda|in|is|not|and|or)\\b';
  const jsKeywords =
    '\\b(function|const|let|var|return|if|else|for|while|switch|case|break|continue|try|catch|finally|throw|class|extends|import|export|from|default|async|await|typeof|instanceof|new|this|interface|type|enum)\\b';
  const sqlKeywords =
    '\\b(SELECT|FROM|WHERE|INSERT|INTO|UPDATE|DELETE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|IN|EXISTS|BETWEEN|LIKE|IS|NULL|CREATE|TABLE|ALTER|DROP|INDEX|VIEW)\\b';
  const cKeywords =
    '\\b(int|char|float|double|void|long|short|unsigned|signed|struct|union|enum|typedef|sizeof|if|else|switch|case|default|break|continue|return|for|while|do|auto|static|extern|const|class|public|private|protected|template|typename|namespace|using|std)\\b';
  const bashKeywords =
    '\\b(echo|cd|ls|mkdir|rm|cp|mv|cat|grep|sed|awk|curl|wget|chmod|chown|sudo|systemctl|export|source|if|then|else|elif|fi|case|esac|for|while|do|done|return|exit)\\b';

  let kw = `${pyKeywords}|${jsKeywords}|${sqlKeywords}|${bashKeywords}`;
  if (['py', 'python'].includes(normLang)) kw = pyKeywords;
  else if (['js', 'jsx', 'ts', 'tsx', 'javascript', 'typescript'].includes(normLang)) kw = jsKeywords;
  else if (['sql'].includes(normLang)) kw = sqlKeywords;
  else if (['c', 'cpp', 'c++', 'h', 'hpp', 'java', 'go', 'rs', 'rust'].includes(normLang)) kw = cKeywords;
  else if (['sh', 'bash', 'zsh', 'shell'].includes(normLang)) kw = bashKeywords;

  const regex = new RegExp(
    `(?<comment>//.*|#.*|--.*)` +
    `|(?<string>"(?:\\\\.|[^"\\\\])*"|'(?:\\\\.|[^'\\\\])*'|\`(?:\\\\.|[^\`\\\\])*\`)` +
    `|(?<keyword>${kw})` +
    `|(?<boolean>\\b(true|false|null|undefined|True|False|None)\\b)` +
    `|(?<number>\\b\\d+(?:\\.\\d+)?\\b)` +
    `|(?<fn>\\b[a-zA-Z_]\\w*(?=\\s*\\())`,
    normLang === 'sql' ? 'gi' : 'g'
  );

  const tokens: Token[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(line)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ type: 'text', text: line.substring(lastIndex, match.index) });
    }
    const groups = match.groups || {};
    if (groups.comment) {
      tokens.push({ type: 'comment', text: match[0] });
    } else if (groups.string) {
      tokens.push({ type: 'string', text: match[0] });
    } else if (groups.keyword) {
      tokens.push({ type: 'keyword', text: match[0] });
    } else if (groups.boolean) {
      tokens.push({ type: 'boolean', text: match[0] });
    } else if (groups.number) {
      tokens.push({ type: 'number', text: match[0] });
    } else if (groups.fn) {
      tokens.push({ type: 'fn', text: match[0] });
    } else {
      tokens.push({ type: 'text', text: match[0] });
    }
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < line.length) {
    tokens.push({ type: 'text', text: line.substring(lastIndex) });
  }

  return tokens.length > 0 ? tokens : [{ type: 'text', text: line }];
};

// ==========================================
// 3. Sub-Components (CodeBlock, Table, Citation)
// ==========================================
interface CodeBlockProps {
  language: string;
  code: string;
  isStreaming?: boolean;
}

const CodeBlock: React.FC<CodeBlockProps> = ({ language, code, isStreaming }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const success = await copyToClipboard(code);
    if (success) {
      setCopied(true);
      message.success('代码已复制到剪贴板');
      setTimeout(() => setCopied(false), 2000);
    } else {
      message.error('复制失败，请手动选取代码');
    }
  };

  const lines = useMemo(() => code.split('\n'), [code]);
  const displayLang = (language || 'TEXT').toUpperCase();

  return (
    <div
      style={{
        margin: '12px 0',
        borderRadius: 8,
        overflow: 'hidden',
        border: '1px solid #1e293b',
        backgroundColor: '#0f172a',
        boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1)',
        maxWidth: '100%',
      }}
    >
      {/* Code Header Bar */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          backgroundColor: '#1e293b',
          padding: '6px 14px',
          borderBottom: '1px solid #334155',
          userSelect: 'none',
        }}
      >
        <span
          style={{
            fontSize: 11.5,
            fontWeight: 600,
            color: '#94a3b8',
            letterSpacing: '0.6px',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <CodeOutlined style={{ fontSize: 12, color: '#38bdf8' }} />
          {displayLang}
        </span>
        <button
          onClick={handleCopy}
          style={{
            background: 'none',
            border: 'none',
            color: copied ? '#4ade80' : '#94a3b8',
            cursor: 'pointer',
            fontSize: 12,
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: '2px 8px',
            borderRadius: 4,
            transition: 'all 0.15s ease',
          }}
          title="复制代码"
        >
          {copied ? <CheckOutlined /> : <CopyOutlined />}
          <span>{copied ? '已复制' : '复制'}</span>
        </button>
      </div>

      {/* Code Content */}
      <div
        style={{
          padding: '12px 16px',
          overflowX: 'auto',
          fontSize: 13,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          lineHeight: 1.6,
          color: '#e2e8f0',
        }}
      >
        <pre style={{ margin: 0, padding: 0, background: 'transparent' }}>
          <code>
            {lines.map((line, idx) => {
              const tokens = tokenizeCodeLine(line, language);
              return (
                <div key={idx} style={{ minHeight: '1.6em' }}>
                  {tokens.map((token, tIdx) => (
                    <span
                      key={tIdx}
                      style={{
                        color: TOKEN_COLORS[token.type],
                        fontStyle: token.type === 'comment' ? 'italic' : 'normal',
                      }}
                    >
                      {token.text}
                    </span>
                  ))}
                  {isStreaming && idx === lines.length - 1 && (
                    <span
                      style={{
                        display: 'inline-block',
                        width: 2,
                        height: 14,
                        backgroundColor: '#38bdf8',
                        marginLeft: 2,
                        verticalAlign: 'text-bottom',
                        animation: 'streamingBlink 1s infinite',
                      }}
                    />
                  )}
                </div>
              );
            })}
          </code>
        </pre>
      </div>
    </div>
  );
};

// ==========================================
// 4. Citation Pill Component
// ==========================================
interface CitationPillProps {
  citationIndex: number;
  citation?: Citation;
  onClick?: (citationIndex: number) => void;
}

const CitationPill: React.FC<CitationPillProps> = ({ citationIndex, citation, onClick }) => {
  const provenance = citation ? getProvenanceDisplay(citation) : null;
  const scorePercent = citation ? (citation.similarity_score * 100).toFixed(0) : null;

  const tooltipContent = citation ? (
    <div style={{ maxWidth: 280, fontSize: 12 }}>
      <div style={{ fontWeight: 600, color: '#ffffff', marginBottom: 3 }}>
        {citation.document_name}
      </div>
      <div style={{ color: '#93c5fd', fontSize: 11, marginBottom: 4 }}>
        {provenance ? provenance.label : `第 ${citation.page_number} 页`}
        {scorePercent ? ` · 匹配度 ${scorePercent}%` : ''}
      </div>
      {citation.content_snippet && (
        <div
          style={{
            color: '#e2e8f0',
            fontSize: 11,
            lineHeight: 1.4,
            maxHeight: 70,
            overflow: 'hidden',
            borderTop: '1px solid rgba(255,255,255,0.15)',
            paddingTop: 4,
          }}
        >
          {citation.content_snippet.slice(0, 100)}...
        </div>
      )}
    </div>
  ) : (
    <span>查看 [Ref-{citationIndex}] 引用来源详情</span>
  );

  return (
    <Tooltip title={tooltipContent} placement="top">
      <span
        onClick={(e) => {
          e.stopPropagation();
          onClick?.(citationIndex);
        }}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 3,
          margin: '0 3px',
          padding: '1px 7px',
          fontSize: 12,
          fontWeight: 600,
          color: '#1d4ed8',
          backgroundColor: '#eff6ff',
          border: '1px solid #bfdbfe',
          borderRadius: 12,
          cursor: 'pointer',
          verticalAlign: 'baseline',
          lineHeight: '1.4',
          userSelect: 'none',
          boxShadow: '0 1px 2px rgba(37,99,235,0.06)',
          transition: 'all 0.15s ease',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.backgroundColor = '#dbeafe';
          e.currentTarget.style.borderColor = '#93c5fd';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.backgroundColor = '#eff6ff';
          e.currentTarget.style.borderColor = '#bfdbfe';
        }}
      >
        <FileTextOutlined style={{ fontSize: 11, color: '#2563eb' }} />
        Ref-{citationIndex}
      </span>
    </Tooltip>
  );
};

// ==========================================
// 5. AST & Block Parser
// ==========================================
type BlockType =
  | { type: 'heading'; level: number; text: string }
  | { type: 'code'; language: string; code: string; isUnclosed?: boolean }
  | { type: 'blockquote'; text: string }
  | { type: 'table'; headers: string[]; alignments: ('left' | 'center' | 'right')[]; rows: string[][] }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'hr' }
  | { type: 'paragraph'; text: string };

const parseBlocks = (content: string): BlockType[] => {
  const blocks: BlockType[] = [];
  const normalized = content.replace(/\r\n/g, '\n');
  const lines = normalized.split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 1. Code Block (```lang)
    const codeStartMatch = line.match(/^```([a-zA-Z0-9_+-]*)/);
    if (codeStartMatch) {
      const language = codeStartMatch[1] || '';
      const codeLines: string[] = [];
      let closed = false;
      i++;
      while (i < lines.length) {
        if (/^```\s*$/.test(lines[i])) {
          closed = true;
          i++;
          break;
        }
        codeLines.push(lines[i]);
        i++;
      }
      blocks.push({
        type: 'code',
        language,
        code: codeLines.join('\n'),
        isUnclosed: !closed,
      });
      continue;
    }

    // 2. Table Block
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[-:\s|]+\|?\s*$/.test(lines[i + 1])) {
      const parseCells = (rowStr: string): string[] => {
        let trimmed = rowStr.trim();
        if (trimmed.startsWith('|')) trimmed = trimmed.substring(1);
        if (trimmed.endsWith('|')) trimmed = trimmed.substring(0, trimmed.length - 1);
        return trimmed.split('|').map((c) => c.trim());
      };

      const headers = parseCells(line);
      const alignLine = parseCells(lines[i + 1]);
      const alignments: ('left' | 'center' | 'right')[] = alignLine.map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        return 'left';
      });

      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        const cells = parseCells(lines[i]);
        rows.push(cells);
        i++;
      }

      blocks.push({ type: 'table', headers, alignments, rows });
      continue;
    }

    // 3. Headings (# H1 ~ ###### H6)
    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      blocks.push({
        type: 'heading',
        level: headingMatch[1].length,
        text: headingMatch[2].trim(),
      });
      i++;
      continue;
    }

    // 4. Horizontal Rule (---, ***, ___)
    if (/^(?:---|\*\*\*|___)\s*$/.test(line)) {
      blocks.push({ type: 'hr' });
      i++;
      continue;
    }

    // 5. Blockquote (> quote)
    if (/^>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      blocks.push({ type: 'blockquote', text: quoteLines.join('\n') });
      continue;
    }

    // 6. Lists (Ordered & Unordered)
    const uListMatch = line.match(/^(\s*)([-*+])\s+(.+)$/);
    const oListMatch = line.match(/^(\s*)(\d+)\.\s+(.+)$/);
    if (uListMatch || oListMatch) {
      const isOrdered = Boolean(oListMatch);
      const items: string[] = [];
      while (i < lines.length) {
        const curLine = lines[i];
        const match = isOrdered
          ? curLine.match(/^(\s*)\d+\.\s+(.+)$/)
          : curLine.match(/^(\s*)[-*+]\s+(.+)$/);
        if (match) {
          items.push(match[2].trim());
          i++;
        } else if (curLine.trim() === '') {
          // Check if list continues after blank line
          if (
            i + 1 < lines.length &&
            (isOrdered
              ? /^\s*\d+\.\s+/.test(lines[i + 1])
              : /^\s*[-*+]\s+/.test(lines[i + 1]))
          ) {
            i++;
          } else {
            break;
          }
        } else {
          break;
        }
      }
      blocks.push({ type: 'list', ordered: isOrdered, items });
      continue;
    }

    // 7. Empty line
    if (line.trim() === '') {
      i++;
      continue;
    }

    // 8. Paragraph (Collect consecutive non-special lines)
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !lines[i].startsWith('```') &&
      !lines[i].match(/^(#{1,6})\s+/) &&
      !lines[i].match(/^>\s?/) &&
      !lines[i].match(/^\s*[-*+]\s+/) &&
      !lines[i].match(/^\s*\d+\.\s+/) &&
      !lines[i].match(/^(?:---|\*\*\*|___)\s*$/)
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    if (paraLines.length > 0) {
      blocks.push({ type: 'paragraph', text: paraLines.join('\n') });
    }
  }

  return blocks;
};

// ==========================================
// 6. Main MarkdownRenderer Component
// ==========================================
export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({
  content,
  citations = [],
  onCitationClick,
  isStreaming = false,
  className,
  style,
}) => {
  // Inline parser with Citation recognition
  const renderInline = (text: string): React.ReactNode[] => {
    const nodes: React.ReactNode[] = [];
    let remaining = text;
    let nodeKey = 0;

    const rules = [
      { type: 'citation', regex: /^\[Ref-(\d+)\]/ },
      { type: 'code', regex: /^`([^`]+)`/ },
      { type: 'bold', regex: /^\*\*([^*]+)\*\*/ },
      { type: 'bold_u', regex: /^__([^_]+)__/ },
      { type: 'strike', regex: /^~~([^~]+)~~/ },
      { type: 'italic', regex: /^\*([^*]+)\*/ },
      { type: 'italic_u', regex: /^_([^_]+)_/ },
      { type: 'link', regex: /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/ },
    ];

    while (remaining.length > 0) {
      let matched = false;

      for (const rule of rules) {
        const match = remaining.match(rule.regex);
        if (match) {
          matched = true;
          const key = `inline-${nodeKey++}`;

          if (rule.type === 'citation') {
            const refIdx = parseInt(match[1], 10);
            const foundCitation = citations.find((c) => c.citation_index === refIdx);
            nodes.push(
              <CitationPill
                key={key}
                citationIndex={refIdx}
                citation={foundCitation}
                onClick={onCitationClick}
              />
            );
          } else if (rule.type === 'code') {
            nodes.push(
              <code
                key={key}
                style={{
                  backgroundColor: '#f1f5f9',
                  color: '#0f172a',
                  padding: '2px 5px',
                  margin: '0 2px',
                  borderRadius: 4,
                  fontSize: 12.5,
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                  border: '1px solid #e2e8f0',
                }}
              >
                {match[1]}
              </code>
            );
          } else if (rule.type === 'bold' || rule.type === 'bold_u') {
            nodes.push(
              <strong key={key} style={{ fontWeight: 600, color: '#0f172a' }}>
                {renderInline(match[1])}
              </strong>
            );
          } else if (rule.type === 'strike') {
            nodes.push(
              <del key={key} style={{ color: '#94a3b8' }}>
                {renderInline(match[1])}
              </del>
            );
          } else if (rule.type === 'italic' || rule.type === 'italic_u') {
            nodes.push(
              <em key={key} style={{ fontStyle: 'italic' }}>
                {renderInline(match[1])}
              </em>
            );
          } else if (rule.type === 'link') {
            nodes.push(
              <a
                key={key}
                href={match[2]}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  color: '#2563eb',
                  textDecoration: 'none',
                  borderBottom: '1px dotted #2563eb',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 3,
                }}
              >
                <LinkOutlined style={{ fontSize: 11 }} />
                {match[1]}
              </a>
            );
          }

          remaining = remaining.slice(match[0].length);
          break;
        }
      }

      if (!matched) {
        const nextSpecial = remaining.search(/\[|`|\*|_|~/);
        if (nextSpecial === -1) {
          nodes.push(remaining);
          break;
        } else if (nextSpecial === 0) {
          nodes.push(remaining[0]);
          remaining = remaining.slice(1);
        } else {
          nodes.push(remaining.slice(0, nextSpecial));
          remaining = remaining.slice(nextSpecial);
        }
      }
    }

    return nodes;
  };

  const blocks = useMemo(() => parseBlocks(content), [content]);

  return (
    <div
      className={className}
      style={{
        fontSize: 14.5,
        lineHeight: 1.75,
        color: '#1e293b',
        wordBreak: 'break-word',
        ...style,
      }}
    >
      <style>{`
        @keyframes streamingBlink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
      `}</style>

      {blocks.map((block, idx) => {
        const isLastBlock = idx === blocks.length - 1;
        const renderStreamingCursor = isStreaming && isLastBlock && block.type !== 'paragraph' && block.type !== 'code';

        const contentNode = (() => {
          switch (block.type) {
            case 'heading': {
              const fontSizes: Record<number, number> = {
                1: 20,
                2: 17,
                3: 15,
                4: 14,
                5: 13.5,
                6: 13,
              };
              const margins: Record<number, string> = {
                1: '18px 0 10px 0',
                2: '16px 0 8px 0',
                3: '14px 0 6px 0',
                4: '12px 0 4px 0',
                5: '10px 0 4px 0',
                6: '8px 0 4px 0',
              };
              return (
                <div
                  key={idx}
                  style={{
                    fontSize: fontSizes[block.level] || 15,
                    fontWeight: 700,
                    margin: margins[block.level] || '12px 0 6px 0',
                    color: '#0f172a',
                    borderBottom: block.level === 1 ? '1px solid #e2e8f0' : 'none',
                    paddingBottom: block.level === 1 ? 6 : 0,
                  }}
                >
                  {renderInline(block.text)}
                </div>
              );
            }

            case 'code':
              return (
                <CodeBlock
                  key={idx}
                  language={block.language}
                  code={block.code}
                  isStreaming={isStreaming && isLastBlock && block.isUnclosed}
                />
              );

            case 'table':
              return (
                <div
                  key={idx}
                  style={{
                    margin: '12px 0',
                    borderRadius: 8,
                    overflowX: 'auto',
                    maxWidth: '100%',
                    border: '1px solid #e2e8f0',
                    boxShadow: '0 1px 2px rgba(0, 0, 0, 0.04)',
                  }}
                >
                  <table
                    style={{
                      width: '100%',
                      borderCollapse: 'collapse',
                      textAlign: 'left',
                      fontSize: 13.5,
                    }}
                  >
                    <thead>
                      <tr style={{ backgroundColor: '#f8fafc', borderBottom: '2px solid #e2e8f0' }}>
                        {block.headers.map((h, hIdx) => (
                          <th
                            key={hIdx}
                            style={{
                              padding: '9px 14px',
                              fontWeight: 600,
                              color: '#1e293b',
                              textAlign: block.alignments[hIdx] || 'left',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {renderInline(h)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {block.rows.map((row, rIdx) => (
                        <tr
                          key={rIdx}
                          style={{
                            backgroundColor: rIdx % 2 === 1 ? '#f8fafc' : '#ffffff',
                            borderBottom: '1px solid #f1f5f9',
                          }}
                        >
                          {row.map((cell, cIdx) => (
                            <td
                              key={cIdx}
                              style={{
                                padding: '8px 14px',
                                color: '#334155',
                                textAlign: block.alignments[cIdx] || 'left',
                              }}
                            >
                              {renderInline(cell)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );

            case 'blockquote':
              return (
                <blockquote
                  key={idx}
                  style={{
                    margin: '10px 0',
                    padding: '8px 14px',
                    borderLeft: '3.5px solid #2563eb',
                    backgroundColor: '#f8fafc',
                    borderRadius: '0 6px 6px 0',
                    color: '#475569',
                  }}
                >
                  {renderInline(block.text)}
                </blockquote>
              );

            case 'list': {
              const ListTag = block.ordered ? 'ol' : 'ul';
              return (
                <ListTag
                  key={idx}
                  style={{
                    margin: '6px 0 10px 0',
                    paddingLeft: 22,
                    color: '#334155',
                  }}
                >
                  {block.items.map((item, itemIdx) => (
                    <li key={itemIdx} style={{ margin: '4px 0', lineHeight: 1.7 }}>
                      {renderInline(item)}
                    </li>
                  ))}
                </ListTag>
              );
            }

            case 'hr':
              return (
                <hr
                  key={idx}
                  style={{
                    border: 'none',
                    borderTop: '1px solid #e2e8f0',
                    margin: '16px 0',
                  }}
                />
              );

            case 'paragraph':
            default:
              return (
                <p
                  key={idx}
                  style={{
                    margin: isLastBlock ? '0 0 4px 0' : '0 0 10px 0',
                    lineHeight: 1.75,
                    color: '#1e293b',
                  }}
                >
                  {renderInline(block.text)}
                  {isStreaming && isLastBlock && (
                    <span
                      style={{
                        display: 'inline-block',
                        width: 2.5,
                        height: 15,
                        backgroundColor: '#2563eb',
                        marginLeft: 4,
                        verticalAlign: 'text-bottom',
                        borderRadius: 1,
                        animation: 'streamingBlink 1s infinite',
                      }}
                    />
                  )}
                </p>
              );
          }
        })();

        if (renderStreamingCursor) {
          return (
            <React.Fragment key={idx}>
              {contentNode}
              <span
                style={{
                  display: 'inline-block',
                  width: 2.5,
                  height: 15,
                  backgroundColor: '#2563eb',
                  marginLeft: 4,
                  verticalAlign: 'text-bottom',
                  borderRadius: 1,
                  animation: 'streamingBlink 1s infinite',
                }}
              />
            </React.Fragment>
          );
        }

        return contentNode;
      })}
    </div>
  );
};

export default MarkdownRenderer;
