# Prompt: Auditoria Completa de Site

> Copie o bloco abaixo e cole na ferramenta/assistente que fará a auditoria. Preencha os campos entre colchetes antes de enviar.

---

Você é um engenheiro de QA e DevOps sênior. Faça uma auditoria completa do site/aplicação a seguir e emita um relatório técnico estruturado.

**Alvo:** [URL / repositório / ambiente]
**Stack:** [ex: React + Node + PostgreSQL, ou "descubra a partir do código"]
**Acessos:** [credenciais de teste, variáveis de ambiente, string de conexão do banco — se aplicável]

## Escopo da verificação

### 1. Frontend
- Todas as páginas/rotas carregam (sem 404/500/tela branca)
- Links internos e externos quebrados
- Erros no console do navegador (JS, CORS, mixed content)
- Imagens, fontes e assets ausentes
- Responsividade (mobile/tablet/desktop)
- Formulários: validação, envio e tratamento de erro

### 2. Backend / APIs
- Todos os endpoints respondem com o status esperado
- Autenticação e autorização funcionando
- Tratamento de erros e mensagens
- Tempos de resposta / latência anormal
- Variáveis de ambiente faltando ou inválidas

### 3. Banco de dados
- Conexão estabelecida
- Integridade de tabelas, índices e chaves estrangeiras
- Migrations pendentes ou falhas
- Queries lentas (slow query log)
- Dados órfãos ou inconsistentes

### 4. Integrações externas
- APIs de terceiros, gateways de pagamento, e-mail, storage
- Webhooks e callbacks
- Chaves/tokens expirados

### 5. Segurança e infraestrutura
- HTTPS/SSL válido e sem expiração próxima
- Headers de segurança (CSP, HSTS, X-Frame-Options)
- Dependências com vulnerabilidades conhecidas
- Exposição de dados sensíveis ou rotas administrativas

## Formato do relatório

Para cada problema encontrado, apresente uma linha na tabela:

| Componente | Problema | Severidade (Crítico/Alto/Médio/Baixo) | Causa provável | Como resolver |
|------------|----------|----------------------------------------|----------------|---------------|
|            |          |                                        |                |               |

Ao final, inclua:
- **Resumo executivo** — visão geral do estado do site
- **Lista priorizada de correções** — ordenada por severidade
- **O que está funcionando corretamente**

Se faltar acesso a alguma camada, indique explicitamente o que não pôde ser testado e o que você precisa para completar a auditoria.
