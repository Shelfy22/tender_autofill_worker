param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$workflow = Get-Content -Raw -Encoding UTF8 -LiteralPath $InputPath | ConvertFrom-Json

function Get-WorkflowNode([string]$Name) {
    $node = $workflow.nodes | Where-Object Name -eq $Name | Select-Object -First 1
    if (-not $node) {
        throw "Workflow node not found: $Name"
    }
    return $node
}

$agent = Get-WorkflowNode 'AI Agent'
$agent.parameters.options | Add-Member -NotePropertyName returnIntermediateSteps -NotePropertyValue $true -Force
$agent.parameters.options.systemMessage = @'
Ты AI-агент подбора товаров из каталога ЭТМ для позиции тендера.

Порядок работы:
1. Сначала вызови инструмент поиска по точному артикулу/коду товара.
2. Если точного совпадения нет, используй Qdrant Product Search.
3. Выбирай по назначению, производителю и существенным техническим характеристикам, а не по цене.
4. Не выдумывай товары и идентификаторы. selectedPointId должен точно совпадать с id/point id одного результата инструмента.
5. «Полное соответствие» ставь только при совпадении назначения и существенных характеристик.
6. «Аналог» ставь только при технически обоснованной замене. Если аналоги запрещены, всё равно верни «Аналог»: итоговая ветка сама отклонит его.
7. Не копируй цену, артикул, ссылку, название, производителя и валюту. Следующая Code-нода возьмёт их программно из payload выбранного результата.
8. Если подходящего результата нет, верни selectedPointId=null и correspondence="Товар не найден".

Верни только валидный JSON:
{"selectedPointId":"точный id выбранного результата или null","correspondence":"Полное соответствие|Аналог|Товар не найден","rationale":"краткое техническое обоснование"}
'@

$parser = Get-WorkflowNode 'Parse Product Match Result'
$parser.parameters.jsCode = @'
function getAgentContent(j) {
  if (!j) return '';
  for (const key of ['output','text','response','content','message','data']) {
    if (typeof j[key] === 'string') return j[key];
  }
  return j.choices?.[0]?.message?.content || j.body?.choices?.[0]?.message?.content || '';
}

function extractJson(text) {
  const source = String(text || '').trim();
  if (!source) return null;
  try { return JSON.parse(source); } catch {}
  const fenced = source.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced?.[1]) {
    try { return JSON.parse(fenced[1].trim()); } catch {}
  }
  const start = source.indexOf('{');
  const end = source.lastIndexOf('}');
  if (start >= 0 && end > start) {
    try { return JSON.parse(source.slice(start, end + 1)); } catch {}
  }
  const arrayStart = source.indexOf('[');
  const arrayEnd = source.lastIndexOf(']');
  if (arrayStart >= 0 && arrayEnd > arrayStart) {
    try { return JSON.parse(source.slice(arrayStart, arrayEnd + 1)); } catch {}
  }
  return null;
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== '') return value;
  }
  return null;
}

function parseNumber(value) {
  if (typeof value === 'number') return Number.isFinite(value) && value >= 0 ? value : null;
  let text = String(value ?? '').replace(/\u00a0/g, ' ').trim();
  if (!text) return null;
  text = text.replace(/[^0-9,.-]/g, '').replace(/(?!^)-/g, '');
  if (!text || text === '-' || !/[0-9]/.test(text)) return null;
  const comma = text.lastIndexOf(',');
  const dot = text.lastIndexOf('.');
  if (comma >= 0 && dot >= 0) {
    const decimal = comma > dot ? ',' : '.';
    text = text.replace(decimal === ',' ? /\./g : /,/g, '').replace(decimal, '.');
  } else if (comma >= 0) {
    const decimals = text.length - comma - 1;
    text = decimals > 0 && decimals <= 2 ? text.replace(/,/g, '.') : text.replace(/,/g, '');
  } else if (dot >= 0) {
    const decimals = text.length - dot - 1;
    if (!(decimals > 0 && decimals <= 2)) text = text.replace(/\./g, '');
  }
  const number = Number(text);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function normalizeCurrency(value) {
  const currency = String(value || '').trim().toUpperCase();
  if (!currency) return null;
  return ['RUR','РУБ','РУБ.','₽'].includes(currency) ? 'RUB' : currency;
}

function catalogIdFromUrl(value) {
  const match = String(value || '').match(/\/cat\/nn\/([^/?#]+)/i);
  return match?.[1] ? String(match[1]).trim() : null;
}

function objectFrom(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value.trim());
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function valueWithPath(sources, keys) {
  for (const [prefix, source] of sources) {
    if (!source || typeof source !== 'object') continue;
    for (const key of keys) {
      const value = source[key];
      if (value !== undefined && value !== null && String(value).trim() !== '') {
        return { value, path: prefix ? `${prefix}.${key}` : key };
      }
    }
  }
  return { value: null, path: '' };
}

function normalizeCandidate(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const payload = raw.payload && typeof raw.payload === 'object' ? raw.payload : raw;
  const document = objectFrom(payload.text) || payload;
  const nestedPayload = document.payload && typeof document.payload === 'object'
    ? document.payload
    : {};
  const metadata = objectFrom(document.metadata) || {};
  const looksLikeProduct =
    Object.keys(metadata).length > 0 ||
    document.pageContent ||
    document.url ||
    document['Ссылка'] ||
    (document.id !== undefined && (document.name || document['Наименование']));
  if (!looksLikeProduct) return null;

  const sources = [
    ['payload.metadata', metadata],
    ['payload', document],
    ['payload.payload', nestedPayload],
    ['point', raw]
  ];
  const productIdResult = valueWithPath(
    sources,
    ['productId','product_id','id','article','Артикул']
  );
  const urlResult = valueWithPath(sources, ['url','link','Ссылка']);
  const productId = String(
    firstDefined(productIdResult.value, catalogIdFromUrl(urlResult.value), '')
  ).trim();
  const pointId = String(
    firstDefined(
      raw.payload !== undefined ? raw.id : null,
      document.id,
      productId,
      ''
    )
  ).trim();
  if (!pointId) return null;

  const priceResult = valueWithPath(
    sources,
    ['price','Медианная цена','Медианная цена, руб.','Цена','medianPrice','median_price']
  );
  const price = parseNumber(priceResult.value);
  const nameResult = valueWithPath(sources, ['name','Наименование','pageContent','content']);
  const manufacturerResult = valueWithPath(
    sources,
    ['vendor','manufacturer','Производитель','brand']
  );
  const currencyResult = valueWithPath(
    sources,
    ['currencyId','currency','currency_id','Валюта']
  );

  return {
    pointId,
    productId: productId || pointId,
    name: String(nameResult.value || '').trim(),
    manufacturer: String(manufacturerResult.value || '').trim(),
    url: String(urlResult.value || '').trim(),
    unitPriceRub: price,
    currency: normalizeCurrency(currencyResult.value) || (price !== null ? 'RUB' : null),
    priceSourceField: price !== null ? priceResult.path : ''
  };
}

const candidates = [];
const candidateIds = new Set();

function collectCandidates(value, depth = 0) {
  if (depth > 12 || value === null || value === undefined) return;
  if (typeof value === 'string') {
    const parsed = extractJson(value);
    if (parsed !== null) collectCandidates(parsed, depth + 1);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectCandidates(item, depth + 1);
    return;
  }
  if (typeof value !== 'object') return;

  const candidate = normalizeCandidate(value);
  if (candidate && !candidateIds.has(candidate.pointId)) {
    candidateIds.add(candidate.pointId);
    candidates.push(candidate);
  }
  for (const [key, nested] of Object.entries(value)) {
    if (key === 'messageLog') continue;
    collectCandidates(nested, depth + 1);
  }
}

collectCandidates($json.intermediateSteps || []);

let original = {};
try { original = $('Loop Tender Products').item.json || {}; } catch { original = {}; }

const parsed = extractJson(getAgentContent($json)) || {};
const selectedPointId = String(
  firstDefined(parsed.selectedPointId, parsed.selected_point_id, parsed.pointId, '')
).trim();
const correspondence = firstDefined(parsed.correspondence, parsed['Соответствие']);
const rationale = String(firstDefined(parsed.rationale, parsed['Обоснование'], '') || '');

let selected = candidates.find(item => item.pointId === selectedPointId) || null;
if (!selected && selectedPointId) {
  selected = candidates.find(item => item.productId === selectedPointId) || null;
}

let match;

if (
  selected &&
  ['Полное соответствие','Аналог'].includes(String(correspondence))
) {
  const sameProduct = candidates.filter(
    item => item.productId && item.productId === selected.productId
  );
  const pricedSameProduct = sameProduct.filter(item => item.unitPriceRub !== null);
  const prices = pricedSameProduct
    .map(item => Number(item.unitPriceRub))
    .filter(Number.isFinite)
    .sort((left, right) => left - right);
  let medianPrice = null;
  if (prices.length) {
    const middle = Math.floor(prices.length / 2);
    medianPrice = prices.length % 2
      ? prices[middle]
      : (prices[middle - 1] + prices[middle]) / 2;
  }
  const sourceFields = [...new Set(
    pricedSameProduct.map(item => item.priceSourceField).filter(Boolean)
  )];
  const selectedHasPrice = selected.unitPriceRub !== null;
  const priceAggregation = pricedSameProduct.length > 1
    ? 'median_same_product_id'
    : pricedSameProduct.length === 1
      ? (selectedHasPrice ? 'selected_candidate' : 'same_product_id_fallback')
      : 'unavailable';
  const priceSourceField = pricedSameProduct.length > 1
    ? `median(${sourceFields.join(', ')})`
    : (sourceFields[0] || '');
  const priceCandidate = pricedSameProduct[0] || selected;
  const article = selected.productId || catalogIdFromUrl(selected.url);
  const link = selected.url || (article ? `https://www.etm.ru/cat/nn/${article}` : null);

  match = {
    'Артикул': article,
    'Ссылка': link,
    'Наименование': selected.name || null,
    'Производитель': selected.manufacturer || null,
    'Медианная цена': medianPrice,
    'Валюта': medianPrice === null ? null : (priceCandidate.currency || 'RUB'),
    'Источник цены': priceSourceField ? `Qdrant: ${priceSourceField}` : '',
    'Поле цены': priceSourceField,
    'Метод цены': priceAggregation,
    'Qdrant point ID': selected.pointId,
    'ID товара': selected.productId,
    'Обоснование': rationale,
    'Соответствие': String(correspondence)
  };
} else {
  // Backward-compatible fallback for executions where intermediateSteps were not returned.
  const legacyPriceRaw = firstDefined(
    parsed['Медианная цена'],
    parsed['Медианная цена, руб.'],
    parsed['Цена'],
    parsed.medianPrice,
    parsed.median_price,
    parsed.price
  );
  const legacyPrice = parseNumber(legacyPriceRaw);
  const legacyCorrespondence = ['Полное соответствие','Аналог'].includes(
    String(parsed['Соответствие'] || parsed.correspondence)
  )
    ? String(parsed['Соответствие'] || parsed.correspondence)
    : 'Товар не найден';
  match = {
    'Артикул': String(parsed['Артикул'] || 'Товар не найден'),
    'Ссылка': String(parsed['Ссылка'] || 'Товар не найден'),
    'Наименование': String(parsed['Наименование'] || 'Товар не найден'),
    'Производитель': String(parsed['Производитель'] || 'Товар не найден'),
    'Медианная цена': legacyPrice,
    'Валюта': legacyPrice === null ? null : String(parsed['Валюта'] || parsed.currency || 'RUB'),
    'Источник цены': legacyPrice === null ? '' : String(
      parsed['Источник цены'] || parsed.priceSource || parsed.price_source || 'Legacy AI Agent output'
    ),
    'Поле цены': legacyPrice === null ? '' : 'legacy_agent_output',
    'Метод цены': legacyPrice === null ? 'unavailable' : 'legacy_agent_output',
    'Qdrant point ID': selectedPointId || null,
    'ID товара': null,
    'Обоснование': rationale || String(parsed['Обоснование'] || 'Товар не найден'),
    'Соответствие': legacyCorrespondence
  };
}

return [{
  json: {
    ...original,
    match,
    catalogSelectionDebug: {
      requestedPointId: selectedPointId || null,
      selectedPointId: match['Qdrant point ID'] || null,
      normalizedCandidateCount: candidates.length,
      priceSourceField: match['Поле цены'] || '',
      priceAggregation: match['Метод цены'] || ''
    }
  }
}];
'@

$summary = Get-WorkflowNode 'Summarize Product Coverage'
$needle = @'
    priceSource:
      String(match['Источник цены'] || ''),

    priceCurrency:
'@
$replacement = @'
    priceSource:
      String(match['Источник цены'] || ''),

    selectedPointId:
      firstDefined(match['Qdrant point ID'], match.selectedPointId),

    productId:
      firstDefined(match['ID товара'], match.productId),

    priceSourceField:
      String(firstDefined(match['Поле цены'], match.priceSourceField, '') || ''),

    priceAggregation:
      String(firstDefined(match['Метод цены'], match.priceAggregation, '') || ''),

    priceCurrency:
'@

if (-not $summary.parameters.jsCode.Contains($needle)) {
    throw 'Expected priceSource block not found in Summarize Product Coverage'
}
$summary.parameters.jsCode = $summary.parameters.jsCode.Replace($needle, $replacement)

$workflow.name = "$($workflow.name) — deterministic Qdrant price"
$outputDirectory = Split-Path -Parent $OutputPath
if ($outputDirectory -and -not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}
$json = $workflow | ConvertTo-Json -Depth 100
Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $json

Write-Output $OutputPath
