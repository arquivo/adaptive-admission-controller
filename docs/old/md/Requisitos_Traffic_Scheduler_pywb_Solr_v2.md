# **Requisitos e desenho técnico: Traffic Scheduler para pywb, Solr e backends do arquivo.pt**

*Documento consolidado da conversa técnica — mitigação de bots distribuídos, prioridades, Redis global, scheduler assíncrono e regras de acesso massivo.*

| Decisão principal: construir uma aplicação intermédia independente, em FastAPI assíncrono, que funcione como scheduler de acesso aos backends. O objetivo não é só fazer rate limit: é ordenar pedidos por prioridade, proteger recursos escassos e degradar tráfego abusivo sem prejudicar utilizadores legítimos. |
| :---- |

# **1\. Resumo executivo**

* O problema inicial é o abuso sobre o pywb do arquivo.pt por tráfego distribuído: muitos pedidos vindos de muitos IPs, frequentemente abaixo dos limites por IP.  
* A defesa baseada apenas em IP é insuficiente. O sistema deve considerar IP, rede /24, ASN/ISP, país, comportamento, padrões de scraping e autenticação.  
* A aplicação intermédia deve receber pedidos vindos do Apache httpd hoje, e futuramente do Caddy, sem acoplar a lógica anti-abuso ao proxy ou ao pywb.  
* O conceito final é um Traffic Scheduler: cada pedido recebe um score, é colocado numa PriorityQueue do backend alvo, e só entra no backend quando houver capacidade.  
* Cada backend deve ter a sua própria fila prioritária e limite de concorrência: por exemplo, uma fila para pywb e outra para Solr.  
* Redis partilhado entre servidores deve ser a fonte de verdade para contadores e scoring global. Cache local por worker pode existir apenas como otimização.

# **2\. Problema a resolver**

O cenário discutido parte de um sistema pywb exposto publicamente, onde existem bots e consumidores intensivos a fazer muitos pedidos distribuídos. Algumas redes e países foram bloqueados, mas isso não resolve o problema estrutural: há ISPs com muitos IPs, e cada IP pode ficar abaixo do limite individual.

* Rate limit por IP é demasiado frágil quando o atacante usa muitos IPs.  
* Bloqueios geográficos podem ser úteis pontualmente, mas são grosseiros e geram risco de falsos positivos.  
* Honeypots não são adequados ao contexto de arquivo da web, porque o HTML arquivado não é controlado de forma fiável.  
* Cache foi excluída como foco desta solução; o objetivo aqui é controlo de acesso/concorrência, não caching.

| Objetivo realista: não tentar eliminar bots completamente. O objetivo é tornar o abuso caro, lento e ineficiente, mantendo boa experiência para utilizadores legítimos. |
| :---- |

# **3\. Arquitetura proposta**

## **3.1 Fase atual**

\[Load Balancer L4 / IP hash\]  
        ↓  
\[Apache httpd\]  
        ↓  
\[FastAPI Traffic Scheduler\]  
        ↓  
\[pywb / Solr / outros backends\]

## **3.2 Fase futura**

\[Load Balancer L4\]  
        ↓  
\[Caddy\]  
        ↓  
\[FastAPI Traffic Scheduler\]  
        ↓  
\[pywb / Solr / outros backends\]

* A lógica crítica deve ficar na aplicação intermédia, não no Apache, Caddy ou pywb.  
* Isto permite trocar Apache httpd por Caddy mais tarde sem reescrever o mecanismo anti-abuso.  
* A aplicação intermédia deve ser tratada como camada de controlo de tráfego, não como feature interna do pywb.

# **4\. Decisões sobre Caddy, Apache e integração no pywb**

* Caddy pode ser estendido via módulos Go, mas isso implica compilar binários customizados e manter lógica complexa dentro do proxy.  
* Caddy é adequado como reverse proxy simples e futuro substituto do Apache httpd, mas não deve ser o local principal para scoring, filas e políticas complexas.  
* Integrar a lógica diretamente no pywb/uWSGI foi considerado possível, mas rejeitado por misturar responsabilidades, manter dependência do modelo WSGI/uWSGI e dificultar evolução.  
* A app separada permite async real, logging próprio, testes isolados e evolução independente.

# **5\. Stack tecnológica da aplicação**

| Componente | Escolha | Justificação |
| :---- | :---- | :---- |
| Framework HTTP | FastAPI | Async nativo, middleware simples, ecossistema maduro. |
| Servidor ASGI | Uvicorn | Adequado a I/O concorrente; evita uWSGI para esta app. |
| Estado global | Redis partilhado | Contadores e scoring globais entre servidores. |
| Cache local | TTL cache por worker | Otimização para lookup ASN e dados não críticos. |
| Cliente HTTP backend | httpx async ou equivalente | Proxy assíncrono para pywb/Solr com streaming quando necessário. |

* A app é I/O bound: recebe pedidos, consulta Redis, calcula score, espera em fila e faz proxy para backend.  
* Redis deve ser usado de forma assíncrona, por exemplo com redis.asyncio.  
* A app deve evitar trabalho CPU-heavy no path principal.

import redis.asyncio as redis

r \= redis.Redis(host="redis-global", decode\_responses=True)  
value \= await r.get("some:key")  
await r.incr("asn:12345:window")

# **6\. Redis, memória local e vários servidores físicos**

A infraestrutura prevê vários servidores físicos com muitos recursos. Cada servidor poderá ter Redis por motivos relacionados com pywb, mas a aplicação de scheduling precisa de uma visão global para detectar ataques distribuídos.

* A aplicação deve usar Redis partilhado/global para contadores de IP, rede /24, ASN, país, utilizador autenticado e backend.  
* Redis local por servidor não é suficiente para mitigação distribuída, porque cada servidor veria apenas uma fração do tráfego.  
* Cache em memória por worker é aceitável para dados locais e não críticos, mas não para decisões globais.  
* Se a app correr com múltiplos processos Uvicorn, cada processo terá a sua memória e fila próprias. Para prioridade global dentro de um nó, começar com um processo async por instância é mais simples.

# **7\. Conceito central: Scheduler, não apenas Rate Limiter**

| O componente deve ser pensado como um scheduler de acesso aos recursos backend. O recurso escasso é a concorrência útil que pywb, Solr ou outro backend conseguem absorver. |
| :---- |

* Cada pedido recebe um score.  
* O pedido é colocado na fila prioritária do backend alvo.  
* Quando existe capacidade, o scheduler retira da fila o pedido com maior prioridade.  
* Pedidos de maior score podem passar à frente de pedidos antigos com menor score.  
* O backend nunca recebe mais pedidos simultâneos do que o limite configurado.

queue \= asyncio.PriorityQueue()

\# score alto deve sair primeiro; usa-se \-score porque PriorityQueue devolve o menor valor  
await queue.put((-score, timestamp, request, future))

# **8\. Suporte a múltiplos backends: pywb, Solr e outros**

A aplicação deve poder controlar múltiplas aplicações backend. Isto faz sentido desde que cada backend tenha a sua própria fila prioritária e o seu próprio limite de concorrência.

                      ┌────────────────────────┐  
                      │ FastAPI Traffic        │  
                      │ Scheduler              │  
                      └───────────┬────────────┘  
                                  │  
             ┌────────────────────┼────────────────────┐  
             ▼                    ▼                    ▼  
      Queue pywb           Queue Solr           Queue outro backend

* Não deve existir uma fila única para toda a plataforma, porque um pico em Solr poderia bloquear pywb, ou vice-versa.  
* Deve existir uma fila única por backend/recurso.  
* O score pode ser calculado de forma centralizada e depois usado para enfileirar no backend correto.  
* Cada backend tem limites próprios, por exemplo pywb=100 em simultâneo, Solr=300, API interna=50. Os números concretos devem ser validados com testes.

# **9\. Score, classes de tráfego e prioridades**

A fila é única por backend, mas não deve ser FIFO. Ela deve ordenar por prioridade: score mais alto primeiro, e timestamp como critério secundário dentro do mesmo score.

| Tipo de tráfego | Score sugerido | Racional |
| :---- | :---- | :---- |
| Utilizador normal anónimo | 100 | Poucos pedidos; deve ter resposta rápida. |
| Investigador autenticado | 80 | Uso legítimo e mais intensivo; passa à frente de bots mas cede a utilizadores ocasionais. |
| Tráfego desconhecido | 40-60 | Sem sinais fortes; prioridade intermédia. |
| Suspeito / ASN problemático | 10-30 | Pode ser degradado sem bloqueio imediato. |
| Bot identificado | 0 ou negativo | Fica no fundo da fila e expira se o sistema estiver cheio. |

* Investigadores autenticados devem ter acesso privilegiado face a bots e tráfego suspeito.  
* Investigadores autenticados devem ter menor prioridade que utilizadores normais ocasionais, para evitar que workloads intensivos prejudiquem a experiência pública básica.  
* Prioridade e quota são conceitos diferentes. Um investigador pode ter maior quota diária ou mais paralelismo permitido, mas menor prioridade que um utilizador ocasional.

# **10\. Starvation e timeout de fila**

Foi discutido aging, lottery scheduling e quotas periódicas, mas a preferência final é manter prioridade rígida e controlar starvation através de timeout de fila.

* Não usar aging como mecanismo principal, porque um pedido mau não deve tornar-se magicamente tão importante como um pedido bom apenas por esperar.  
* Manter ordenação por score e timestamp: maior score primeiro; dentro do mesmo score, FIFO.  
* Aceitar que pedidos de score baixo podem não ser servidos durante períodos de saturação. Isso é coerente com o objetivo de proteger recursos.  
* Definir timeout de fila de 5 minutos. Se o pedido não entrar no backend nesse período, deve receber resposta de erro controlada.

QUEUE\_TIMEOUT\_SECONDS \= 300   \# 5 minutos  
BACKEND\_TIMEOUT\_SECONDS \= 120 \# exemplo para pywb; ajustar com testes

| Modelo preferido: PriorityQueue(score rígido) \+ timestamp \+ timeout de fila de 5 minutos \+ limite de concorrência por backend. Sem aging, sem multi-filas por classe, sem lotarias. |
| :---- |

# **11\. Ciclo de vida de um pedido**

1. Pedido chega ao Apache/Caddy e é encaminhado para a app FastAPI.  
2. FastAPI identifica backend alvo: pywb, Solr ou outro.  
3. A app calcula score com base em sinais locais e globais.  
4. A app cria um Future e coloca o pedido na PriorityQueue do backend.  
5. O pedido aguarda até ser escolhido pelo scheduler ou até expirar por timeout de fila.  
6. Um worker interno retira o pedido mais prioritário disponível.  
7. O worker faz proxy assíncrono para o backend.  
8. A resposta é devolvida ao cliente; métricas são registadas.

future \= asyncio.get\_running\_loop().create\_future()  
await backend\_queue.put((-score, timestamp, request, future))

try:  
    response \= await asyncio.wait\_for(future, timeout=QUEUE\_TIMEOUT\_SECONDS)  
except asyncio.TimeoutError:  
    return busy\_response()

return response

# **12\. Sinais para cálculo de score**

* Categoria do utilizador: anónimo, investigador autenticado, tráfego desconhecido, bot identificado.  
* IP individual: útil, mas nunca suficiente isoladamente.  
* Rede /24 IPv4 ou prefixo IPv6 equivalente: útil para tráfego distribuído em IPs próximos.  
* ASN/ISP: sinal essencial para tráfego distribuído por muitos IPs dentro da mesma rede.  
* País: sinal auxiliar e grosseiro; evitar decisões cegas apenas por país.  
* User-Agent, headers e cookies: sinais fracos mas baratos.  
* Padrões de scraping: varrimento sequencial de timestamps, padrões regulares, volume por URL/coleção.  
* Histórico recente em Redis: contadores por IP, rede, ASN, país, utilizador, backend e janela temporal.  
* Autenticação de investigadores: prioridade específica e quotas separadas.

priority\_score \= base\_score\_for\_user\_class  
priority\_score \-= penalty\_ip  
priority\_score \-= penalty\_net24  
priority\_score \-= penalty\_asn  
priority\_score \-= penalty\_country  
priority\_score \-= penalty\_behavior

# **13\. Regras de limitação de acesso massivo e penalização dinâmica**

Esta secção define as regras base para reduzir a prioridade de pedidos subsequentes quando o tráfego atinge determinados níveis numa janela temporal curta. A ideia é medir volume por várias dimensões, calcular penalizações e subtrair essas penalizações ao score base do pedido.

| Regra conceptual: score final \= score base da classe do utilizador \- penalizações por IP \- penalizações por rede /24 \- penalizações por ASN/ISP \- penalizações por país \- penalizações comportamentais. Quanto menor o score final, menor a prioridade na fila. |
| :---- |

## **13.1 Dimensões de controlo**

* IP individual: versão básica e imediata. Útil para clientes agressivos que concentram volume num único IP.  
* Rede /24 em IPv4: agrupa clientes que rodam dentro do mesmo prefixo. Ajuda quando um actor usa muitos IPs próximos.  
* Rede equivalente em IPv6: usar prefixo configurável, por exemplo /48 ou /56, a validar conforme tráfego real.  
* ASN/ISP: dimensão mais importante para tráfego distribuído por muitos IPs dentro do mesmo operador ou rede.  
* País: camada grosseira, útil apenas como penalização adicional quando existe abuso forte por origem geográfica; evitar bloqueios cegos.  
* Utilizador autenticado: investigadores autenticados devem ser avaliados também, mas a partir de um score base diferente e com limites/quota próprios.  
* Backend alvo: cada backend pode ter limiares diferentes; pywb, Solr e outros serviços não têm o mesmo custo.

## **13.2 Janelas temporais e limiares**

Os limiares abaixo são exemplos iniciais para implementar e testar. Devem ser afinados com métricas reais de produção.

| Dimensão | Janela | Contador Redis | Quando excede | Penalização sugerida |
| :---- | :---- | :---- | :---- | :---- |
| IP | 10s / 60s | rl:ip:{ip}:{backend} | muitos pedidos do mesmo IP | \-10 a \-40 |
| Rede IPv4 /24 | 10s / 60s | rl:net24:{prefix}:{backend} | muitos IPs no mesmo /24 | \-10 a \-40 |
| Rede IPv6 /48 ou /56 | 10s / 60s | rl:net6:{prefix}:{backend} | muitos IPs no mesmo prefixo IPv6 | \-10 a \-40 |
| ASN/ISP | 10s / 60s | rl:asn:{asn}:{backend} | pico agregado do operador/rede | \-20 a \-70 |
| País | 60s / 300s | rl:country:{cc}:{backend} | pico agregado por país | \-5 a \-30 |
| Utilizador autenticado | 60s / diário | rl:user:{id}:{backend} | uso acima de quota ou paralelismo | \-5 a \-40 |

## **13.3 Fórmula de score final**

O score base vem da classe do pedido. Depois são somadas penalizações por volume nas várias dimensões. O score final determina a posição na PriorityQueue do backend alvo.

base\_score \= score\_por\_classe(request)

penalty \= 0  
penalty \+= penalty\_ip(ip, backend)  
penalty \+= penalty\_rede\_24(prefix24, backend)  
penalty \+= penalty\_asn(asn, backend)  
penalty \+= penalty\_pais(country, backend)  
penalty \+= penalty\_utilizador(user\_id, backend)  
penalty \+= penalty\_comportamento(request)

final\_score \= clamp(base\_score \- penalty, min\_score=-100, max\_score=100)

## **13.4 Penalização dinâmica por aumento de volume**

Quando os pedidos numa dada dimensão atingem um limiar dentro de N segundos, os pedidos subsequentes dessa mesma dimensão recebem menor prioridade. Isto não bloqueia necessariamente o tráfego: empurra-o para o fundo da fila.

* Se um IP exceder X pedidos em 10 segundos, os pedidos seguintes desse IP recebem penalização.  
* Se uma rede /24 exceder Y pedidos em 60 segundos, todos os IPs desse prefixo recebem penalização.  
* Se um ASN exceder Z pedidos em 60 segundos, os pedidos desse ASN passam a ter prioridade reduzida.  
* Se um país gerar tráfego anómalo acima de um limiar, aplicar apenas penalização suave; país deve ser sinal auxiliar, não decisão principal.  
* As penalizações devem ter TTL curto para o sistema recuperar automaticamente quando o pico acaba.

\# Pseudocódigo conceptual  
count \= await redis.incr(key)  
if count \== 1:  
    await redis.expire(key, window\_seconds)

if count \> hard\_threshold:  
    penalty \= high\_penalty  
elif count \> soft\_threshold:  
    penalty \= low\_penalty  
else:  
    penalty \= 0

## **13.5 Esquema de keys Redis sugerido**

rl:ip:{ip}:{backend}  
rl:net24:{ipv4\_prefix\_24}:{backend}  
rl:net6:{ipv6\_prefix}:{backend}  
rl:asn:{asn}:{backend}  
rl:country:{country\_code}:{backend}  
rl:user:{user\_id}:{backend}  
rl:behavior:{fingerprint\_or\_pattern}:{backend}

* Usar TTL curto nos contadores de janela curta, por exemplo 10, 30 ou 60 segundos.  
* Usar janelas maiores para métricas e quotas, por exemplo 5 minutos ou diário para utilizadores autenticados.  
* As chaves devem incluir o backend para permitir limiares diferentes em pywb e Solr.  
* Redis global continua obrigatório para que os contadores sejam partilhados entre servidores físicos.

## **13.6 Exemplos de comportamento esperado**

| Cenário | Sinal detetado | Efeito no score | Resultado na fila |
| :---- | :---- | :---- | :---- |
| Cliente agressivo único | IP excede limiar em 10s | penalização IP | pedidos seguintes descem na prioridade |
| Muitos IPs próximos | /24 excede limiar | penalização rede | todo o prefixo perde prioridade |
| ISP distribui pedidos | ASN excede limiar | penalização ASN forte | pedidos do ASN passam atrás de tráfego normal |
| Pico por país | país excede limiar | penalização suave | degradação auxiliar, não bloqueio cego |
| Investigador autenticado intensivo | utilizador excede quota/paralelismo | penalização moderada | continua acima de bots, mas abaixo de humanos ocasionais |

## **13.7 Regras de implementação**

* Começar por IP, /24, ASN e backend. País deve entrar como sinal fraco, não como regra principal.  
* O score deve diminuir quando há abuso. Como a PriorityQueue ordena por score maior primeiro, menor score significa menor prioridade.  
* Não bloquear de imediato salvo casos extremos; preferir degradar prioridade e deixar o timeout de fila expirar pedidos abusivos.  
* Evitar penalizações permanentes. Usar TTLs e janelas móveis para recuperação automática.  
* Registar sempre a decomposição do score: base\_score, penalty\_ip, penalty\_net24, penalty\_asn, penalty\_country, penalty\_user, final\_score.  
* Separar thresholds por backend: Solr e pywb podem ter custos e tolerâncias muito diferentes.

| A regra mais importante para tráfego massivo é penalizar dimensões agregadas, não apenas IPs individuais. IP é a versão básica; /24 e ASN/ISP são essenciais para tráfego distribuído; país é apenas sinal auxiliar. Quando uma dimensão excede limites numa janela temporal, os pedidos subsequentes dessa dimensão perdem score e prioridade. |
| :---- |

# **14\. Exemplo operacional**

Exemplo com pywb configurado para 100 pedidos simultâneos:

* Chegam 1000 pedidos de score baixo: 100 entram no pywb, 900 ficam pendentes.  
* Chegam depois 50 pedidos de score alto: entram na mesma fila do pywb, mas ficam à frente dos 900 pedidos de score baixo.  
* Quando slots libertam, os 50 pedidos de score alto são servidos antes dos pedidos antigos de score baixo.  
* Se pedidos de score baixo não forem servidos em 5 minutos, expiram com resposta controlada.

# **15\. Métricas e observabilidade**

* queue\_wait\_time por backend e classe de tráfego.  
* queue\_depth por backend.  
* requests\_executing por backend.  
* dropped\_due\_queue\_timeout.  
* backend\_timeout\_count.  
* score\_distribution.  
* decomposição de score por dimensão: IP, /24, ASN, país, utilizador e comportamento.  
* top ASN por volume e por expirados.  
* top /24 por volume e por expirados.  
* utilizadores autenticados: volume, tempo médio de espera e taxa de timeout.

| Métrica especialmente útil: P95/P99 de tempo em fila por backend. Se P95 ou P99 subir muito, há saturação ou ataque em curso. |
| :---- |

# **16\. Implementação inicial recomendada**

9. Criar aplicação FastAPI com um ResourceScheduler por backend.  
10. Começar com um processo Uvicorn por instância para manter uma fila global dentro do processo.  
11. Usar Redis async para estado global e scoring.  
12. Implementar fila prioritária por backend com timeout de 5 minutos.  
13. Implementar limite de concorrência por backend.  
14. Implementar scoring básico por IP, /24, ASN e backend.  
15. Adicionar país como penalização auxiliar e não como regra principal.  
16. Implementar autenticação de investigadores e base score próprio.  
17. Registar decomposição de score desde o início.  
18. Testar com carga sintética antes de ativar produção.

Resource(name="pywb", concurrency=100, queue\_timeout=300)  
Resource(name="solr", concurrency=300, queue\_timeout=300)

# **17\. Não objetivos / decisões rejeitadas**

* Não implementar lógica complexa dentro do pywb.  
* Não depender exclusivamente de IP.  
* Não usar Redis local por servidor como fonte de verdade global.  
* Não começar com múltiplas filas por classe de tráfego se uma fila prioritária por backend for suficiente.  
* Não usar aging como mecanismo principal de anti-starvation.  
* Não resolver este problema através de cache.  
* Não apostar em honeypots para este contexto de arquivo da web.  
* Não usar país como único critério de penalização ou bloqueio, salvo exceções operacionais específicas.

# **18\. Conclusão**

| A solução consolidada é uma aplicação FastAPI assíncrona que atua como Traffic Scheduler. Ela calcula score, enfileira por backend, prioriza pedidos legítimos, dá prioridade intermédia a investigadores autenticados, degrada bots, aplica timeout de 5 minutos na fila e protege pywb/Solr com limites rígidos de concorrência. As novas regras de acesso massivo penalizam dinamicamente IP, /24, ASN/ISP e país quando há excesso numa janela temporal. |
| :---- |

Esta abordagem permite evoluir de Apache httpd para Caddy sem alterar a lógica principal, e cria uma camada reutilizável para múltiplos backends do ecossistema arquivo.pt.