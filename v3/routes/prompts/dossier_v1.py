"""
Dossier Route - Gemini Synthesis Prompt (v1)
Gemini 3 Pro Preview long-context dossier synthesis
"""

DOSSIER_SYSTEM = """You are a research analyst synthesizing evidence for prediction markets.

You are given a dossier of documents, evidence items, and resolution rules. Your job:

1. Synthesize all evidence into a coherent picture
2. Identify contradictions between sources
3. Assess overall source quality (reliable publishers vs social media)
4. Estimate the probability of YES resolution
5. Flag any evidence gaps that would change your estimate

IMPORTANT:
- You do NOT see the current market price
- Weight evidence by reliability score and freshness
- Note contradictions explicitly
- If evidence is thin, say so with high uncertainty

Output ONLY valid JSON:
{
  "p_hat": 0.45,
  "uncertainty": 0.20,
  "evidence_ids": ["e1", "e2", "e3"],
  "reasoning_summary": "...",
  "contradictions": ["Source A says X but Source B says Y"],
  "source_quality": 0.7,
  "key_documents": ["doc_1", "doc_2"]
}"""


def build_dossier_synthesis_prompt(
    question: str,
    rules: str,
    documents: list[dict],
    evidence: list[dict],
    clarifications: list[str]
) -> str:
    """
    Build the user prompt for Gemini long-context dossier synthesis
    
    Args:
        question: Market question
        rules: Resolution rules
        documents: List of source document dicts with keys: doc_id, title, source_type, publisher, content_summary
        evidence: List of evidence item dicts with keys: evidence_id, claim, polarity, reliability, doc_id
        clarifications: List of clarification texts
        
    Returns:
        Formatted prompt string
    """
    # Format documents
    doc_lines = []
    for i, doc in enumerate(documents, 1):
        doc_id = doc.get("doc_id", f"doc_{i}")
        title = doc.get("title", "Untitled")
        source_type = doc.get("source_type", "unknown")
        publisher = doc.get("publisher", "Unknown")
        content = doc.get("content_summary", doc.get("content", "(No content)"))
        
        doc_lines.append(f"""
DOCUMENT [{doc_id}]
Type: {source_type}
Publisher: {publisher}
Title: {title}
Content:
{content}
---""")
    
    documents_block = "\n".join(doc_lines) if doc_lines else "(No documents available)"
    
    # Format evidence items
    evidence_lines = []
    for i, item in enumerate(evidence, 1):
        eid = item.get("evidence_id", f"e{i}")
        claim = item.get("claim", "")
        polarity = item.get("polarity", "NEUTRAL")
        reliability = item.get("reliability", 0.5)
        doc_id = item.get("doc_id", "unknown")
        
        evidence_lines.append(
            f"[{eid}] (from {doc_id}, {polarity}, reliability={reliability:.2f}):\n  {claim}"
        )
    
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "(No evidence available)"
    
    # Format clarifications
    clarifications_block = ""
    if clarifications:
        clarifications_block = "\n\nCLARIFICATIONS:\n" + "\n".join(
            f"- {c}" for c in clarifications
        )
    
    prompt = f"""MARKET QUESTION:
{question}

RESOLUTION RULES:
{rules}{clarifications_block}

SOURCE DOCUMENTS:
{documents_block}

EXTRACTED EVIDENCE:
{evidence_block}

TASK:
Synthesize all source documents and evidence to estimate the probability that this market resolves YES.

Consider:
1. Do documents contradict each other?
2. Are sources reliable (e.g., SEC filings vs. social media)?
3. Is evidence recent and relevant?
4. What key facts support YES? What supports NO?
5. What evidence gaps remain?

Output ONLY the JSON object, nothing else."""
    
    return prompt
