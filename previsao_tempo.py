# -*- coding: utf-8 -*-
"""
Previsão do tempo da LT JAN (Janaúba 6 – Presidente Juscelino).

Gera previsao_tempo.js (lido pelo Diagrama de Avanço LT JAN.html) com:
  - chuva em grade 0,25° a cada 3 h por 7 dias e rajada máxima diária (modelo ECMWF IFS, Open Data);
  - previsão do INMET por município (dias 1–5) e avisos meteorológicos ativos.

Fonte do modelo: "Adapted from 'ECMWF IFS 15-day Forecast Data' by ECMWF,
licensed under CC BY 4.0, available at https://data.ecmwf.int/forecasts/".

Uso:  pythonw previsao_tempo.py          (rodado pelo Agendador de Tarefas)
      python  previsao_tempo.py --forcar (baixa de novo mesmo sem rodada nova)
"""
import os, sys, json, math, time, datetime as dt, traceback
from concurrent.futures import ThreadPoolExecutor

AQUI = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.environ.get('LTJAN_SAIDA') or os.path.join(AQUI, 'previsao_tempo.js')   # na nuvem: pasta publicada
LOG = os.environ.get('LTJAN_LOG') or os.path.join(AQUI, 'log.txt')
TZ = dt.timezone(dt.timedelta(hours=-3))          # Minas Gerais, sem horário de verão
PREFIXO = 'window.LTJAN_WX = '

# grade alinhada ao 0,25° do ECMWF cobrindo a LT com folga
LA0, LA1, LO0, LO1, ST = -19.25, -15.75, -45.50, -42.25, 0.25
NLA = int(round((LA1 - LA0) / ST)) + 1
NLO = int(round((LO1 - LO0) / ST)) + 1
NDIAS = 7

ESPELHOS = os.environ.get('LTJAN_ESPELHO', '').split() or [
    'https://data.ecmwf.int/forecasts',
    'https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com',
    'https://ai4edataeuwest.blob.core.windows.net/ecmwf',
]
INMET = os.environ.get('LTJAN_INMET', 'https://apiprevmet3.inmet.gov.br')
PRAIO = 81                       # passos (h) usados no índice de raios: cobre os 3 primeiros dias locais
PASSOS = list(range(0, 145, 3)) + list(range(150, 175, 6))   # cobre até 00h local do 8º dia

MUN = [  # código IBGE, nome, lat/lon (centro das torres no município), trechos
    (3126703, 'Francisco Sá', -16.4125, -43.6327, ['MOC']),
    (3143302, 'Montes Claros', -16.7758, -43.7458, ['MOC']),
    (3107307, 'Bocaiúva', -17.2460, -43.8164, ['MOC']),
    (3109204, 'Buenópolis', -17.7801, -44.0036, ['BUE', 'MOC']),
    (3104809, 'Augusto de Lima', -18.1005, -44.0910, ['BUE']),
    (3142502, 'Monjolos', -18.3332, -44.0852, ['BUE']),
    (3160603, 'Santo Hipólito', -18.5027, -44.0887, ['BUE']),
    (3153202, 'Presidente Juscelino', -18.6244, -44.0964, ['BUE']),
]

import requests
SESS = requests.Session()
SESS.headers['User-Agent'] = 'LTJAN-previsao/1.0'


def log(msg):
    linha = f'{dt.datetime.now(TZ):%d/%m/%Y %H:%M:%S}  {msg}'
    print(linha, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(linha + '\n')
        # mantém o log curto
        if os.path.getsize(LOG) > 40_000:          # ~1 semana de execuções
            with open(LOG, encoding='utf-8') as f:
                ls = f.readlines()[-300:]
            with open(LOG, 'w', encoding='utf-8') as f:
                f.writelines(ls)
    except Exception:
        pass


def get(url, tent=3, **kw):
    ult = None
    for k in range(tent):
        try:
            r = SESS.get(url, timeout=60, **kw)
            if r.status_code in (200, 206):
                return r
            if r.status_code == 404:
                return None
            ult = f'HTTP {r.status_code}'
        except Exception as e:
            ult = str(e)
        time.sleep(2 + 3 * k)
    raise RuntimeError(f'{url}: {ult}')


def ler_anterior():
    try:
        with open(SAIDA, encoding='utf-8') as f:
            s = f.read().strip()
        return json.loads(s[len(PREFIXO):].rstrip(';'))
    except Exception:
        return None


# ---------------------------------------------------------------- ECMWF
def url_run(base, run, passo, ext):
    return f'{base}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{passo}h-oper-fc.{ext}'


def achar_rodada():
    agora = dt.datetime.now(dt.timezone.utc)
    cands = []
    d0 = agora.replace(hour=12 if agora.hour >= 12 else 0, minute=0, second=0, microsecond=0)
    for k in range(6):
        cands.append(d0 - dt.timedelta(hours=12 * k))
    for base in ESPELHOS:
        try:
            for run in cands:
                r = SESS.head(url_run(base, run, PASSOS[-1], 'index'), timeout=30, allow_redirects=True)
                if r.status_code == 200:
                    return base, run
        except Exception as e:
            log(f'  espelho {base} indisponível: {e}')
    raise RuntimeError('nenhuma rodada ECMWF encontrada nos espelhos')


def ler_indice(base, run, passo):
    r = get(url_run(base, run, passo, 'index'))
    if r is None:
        raise RuntimeError(f'índice do passo {passo} não existe')
    out = {}
    for ln in r.text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        j = json.loads(ln)
        if j.get('levtype') == 'sfc':
            out[j['param']] = (int(j['_offset']), int(j['_length']))
    return out


def baixar_campo(base, run, passo, off, ln):
    r = get(url_run(base, run, passo, 'grib2'), headers={'Range': f'bytes={off}-{off + ln - 1}'})
    if r is None or len(r.content) != ln:
        raise RuntimeError(f'campo incompleto no passo {passo}')
    return r.content


_SUB = {}


def recortar(msg):
    import eccodes
    import numpy as np
    h = eccodes.codes_new_from_message(msg)
    try:
        ni = eccodes.codes_get(h, 'Ni'); nj = eccodes.codes_get(h, 'Nj')
        la1 = eccodes.codes_get(h, 'latitudeOfFirstGridPointInDegrees')
        lo1 = eccodes.codes_get(h, 'longitudeOfFirstGridPointInDegrees')
        di = eccodes.codes_get(h, 'iDirectionIncrementInDegrees')
        dj = eccodes.codes_get(h, 'jDirectionIncrementInDegrees')
        jpos = eccodes.codes_get(h, 'jScansPositively')
        v = eccodes.codes_get_values(h).reshape(nj, ni)
    finally:
        eccodes.codes_release(h)
    chave = (ni, nj, la1, lo1, di, dj, jpos)
    if chave not in _SUB:
        lats = la1 + (dj if jpos else -dj) * np.arange(nj)
        lons = ((lo1 + di * np.arange(ni)) + 180) % 360 - 180
        ii = [int(np.argmin(abs(lats - (LA0 + k * ST)))) for k in range(NLA)]
        jj = [int(np.argmin(abs(lons - (LO0 + k * ST)))) for k in range(NLO)]
        _SUB[chave] = (ii, jj)
    ii, jj = _SUB[chave]
    return v[np.ix_(ii, jj)]          # [lat asc][lon asc]


def modelo(forcar, anterior):
    import numpy as np
    base, run = achar_rodada()
    run_txt = f'{run:%Y-%m-%dT%H}Z'
    if not forcar and anterior and anterior.get('modelo') and anterior['modelo'].get('rodada') == run_txt and 'raio3' in anterior['modelo']:
        log(f'ECMWF: rodada {run_txt} já está no arquivo — mantém')
        return anterior['modelo']
    log(f'ECMWF: rodada {run_txt} em {base}')
    idx = {}
    with ThreadPoolExecutor(8) as ex:
        for p, d in zip(PASSOS, ex.map(lambda p: ler_indice(base, run, p), PASSOS)):
            idx[p] = d
    GUST = ['10fg3', '10fg', '10fg6', 'i10fg']
    log('  vento no passo 3: ' + ','.join(sorted(k for k in idx[3] if 'fg' in k or k in ('10u', '10v'))) +
        ' | passo 6: ' + ','.join(sorted(k for k in idx[6] if 'fg' in k or k in ('10u', '10v'))))
    graj = {p: next((q for q in GUST if q in idx[p]), None) for p in PASSOS[1:]}
    if sum(1 for v in graj.values() if v) >= len(PASSOS) // 3:
        praj = None; rotulo_raj = 'rajada máxima a 10 m'
    else:
        praj = ['10u', '10v']; rotulo_raj = 'vento médio a 10 m (sem rajada no Open Data)'
        graj = {p: None for p in PASSOS[1:]}
    log(f'  vento: {rotulo_raj}; passos com rajada: {sum(1 for v in graj.values() if v)}/{len(PASSOS) - 1}')

    tarefas = []
    for p in PASSOS:
        tarefas.append(('tp', p))
        if p <= PRAIO and 'cp' in idx[p]: tarefas.append(('cp', p))           # chuva convectiva (índice de raios)
        if p <= PRAIO and 'mucape' in idx[p]: tarefas.append(('mucape', p))   # energia para tempestade (CAPE)
        if p > 0:
            tarefas += [(q, p) for q in (praj or [graj[p]]) if q]
    tot = sum(idx[p][q][1] for q, p in tarefas)
    log(f'  baixando {len(tarefas)} campos ({tot / 1e6:.0f} MB)')

    def um(t):
        q, p = t
        off, ln = idx[p][q]
        return t, baixar_campo(base, run, p, off, ln)
    brutos = {}
    with ThreadPoolExecutor(6) as ex:                 # só download em paralelo
        for t, b in ex.map(um, tarefas):
            brutos[t] = b
    log('  download concluído; decodificando')
    campos = {t: recortar(b) for t, b in brutos.items()}   # eccodes sempre na thread principal
    del brutos

    # séries locais: início = 00h local do dia de (rodada + 3 h)
    run_loc = run.astimezone(TZ)
    dia0 = (run_loc + dt.timedelta(hours=3)).date()
    t0 = dt.datetime.combine(dia0, dt.time(0), TZ)
    desloc = int((t0 - run).total_seconds() // 3600)          # passo UTC da hora local 0
    nh = 24 * NDIAS
    hora = np.full((nh, NLA, NLO), np.nan)                       # chuva mm/h
    for a, b in zip(PASSOS[:-1], PASSOS[1:]):
        mm = np.clip((campos[('tp', b)] - campos[('tp', a)]) * 1000.0, 0, None) / (b - a)
        for s in range(a, b):
            h = s - desloc
            if 0 <= h < nh:
                hora[h] = mm
    rj_h = np.full((nh, NLA, NLO), np.nan)                       # km/h
    pv = [p for p in PASSOS[1:] if praj or graj[p]]
    for a, b in zip([0] + pv[:-1], pv):
        v = np.hypot(campos[('10u', b)], campos[('10v', b)]) if praj else campos[(graj[b], b)]
        for s in range(a, b):
            h = s - desloc
            if 0 <= h < nh:
                rj_h[h] = np.fmax(rj_h[h], v * 3.6)
    # ---- índice de risco de tempestade com raios (dias 1–3): chuva convectiva em 3 h × CAPE
    nr = 24                                                      # 3 dias × 8 janelas de 3 h
    cph = np.full((nh, NLA, NLO), np.nan); capeh = np.full((nh, NLA, NLO), np.nan)
    pr_ok = [p for p in PASSOS if p <= PRAIO and ('cp', p) in campos]
    for a, b in zip(pr_ok[:-1], pr_ok[1:]):
        mm = np.clip((campos[('cp', b)] - campos[('cp', a)]) * 1000.0, 0, None) / (b - a)
        ca = None
        if ('mucape', b) in campos: ca = campos[('mucape', b)] if ('mucape', a) not in campos else np.fmax(campos[('mucape', a)], campos[('mucape', b)])
        for s_ in range(a, b):
            h = s_ - desloc
            if 0 <= h < nh:
                cph[h] = mm
                if ca is not None: capeh[h] = ca
    raio = []
    for k in range(nr):
        blk = slice(3 * k, 3 * k + 3)
        if np.all(np.isnan(cph[blk])): raio.append(np.full((NLA, NLO), np.nan)); continue
        cp3 = np.nansum(cph[blk], axis=0); cape = np.nan_to_num(np.nanmax(capeh[blk], axis=0))
        r = cp3 * np.clip(cape / 1000.0, 0, 2) * 0.6
        r[cape < 200] = 0
        raio.append(r)
    raio = np.array(raio)
    n3 = nh // 3
    c3 = np.array([np.nansum(hora[3 * k:3 * k + 3], axis=0) if not np.all(np.isnan(hora[3 * k:3 * k + 3])) else np.full((NLA, NLO), np.nan) for k in range(n3)])
    grade = []
    for i in range(NLA):
        for j in range(NLO):
            grade.append([None if math.isnan(x) else round(float(x), 1) for x in c3[:, i, j]])
    graio, graj = [], []
    rj3 = np.array([np.nanmax(rj_h[3 * k:3 * k + 3], axis=0) if not np.all(np.isnan(rj_h[3 * k:3 * k + 3])) else np.full((NLA, NLO), np.nan) for k in range(n3)])
    for i in range(NLA):
        for j in range(NLO):
            graio.append([None if math.isnan(x) else round(float(x), 1) for x in raio[:, i, j]])
            graj.append([None if math.isnan(x) else int(round(float(x))) for x in rj3[:, i, j]])
    dias = [(dia0 + dt.timedelta(days=k)).isoformat() for k in range(NDIAS)]
    t3 = [(t0 + dt.timedelta(hours=3 * k)).strftime('%Y-%m-%dT%H:%M') for k in range(n3)]
    hora_ini = max(0, -desloc)                                     # 1ª hora local com dado

    def interp(a2, la, lo):
        fi = (la - LA0) / ST; fj = (lo - LO0) / ST
        i0 = min(NLA - 2, max(0, int(fi))); j0 = min(NLO - 2, max(0, int(fj)))
        ti = fi - i0; tj = fj - j0
        return (a2[i0, j0] * (1 - ti) * (1 - tj) + a2[i0 + 1, j0] * ti * (1 - tj)
                + a2[i0, j0 + 1] * (1 - ti) * tj + a2[i0 + 1, j0 + 1] * ti * tj)

    mun = []
    for g, n, la, lo, sec in MUN:
        mm_d, rj_d, cod = [], [], []
        for k in range(NDIAS):
            bl = slice(24 * k, 24 * k + 24)
            m = float(interp(np.nansum(hora[bl], axis=0), la, lo))
            r = float(interp(np.nanmax(rj_h[bl], axis=0), la, lo))
            mm_d.append(round(m, 1)); rj_d.append(round(r))
            cod.append(95 if (m >= 10 and r >= 60) else 63 if m >= 10 else 53 if m >= 1 else 2)
        mun.append({'g': g, 'time': dias, 'precipitation_sum': mm_d, 'wind_gusts_10m_max': rj_d, 'weather_code': cod})
    log(f'  ok: {len(grade)} pontos × {n3} intervalos de 3 h; dias {dias[0]} a {dias[-1]}; raios: {"sim" if len(pr_ok) > 1 else "sem cp/mucape"} (máx {np.nanmax(raio) if len(pr_ok) > 1 else 0:.1f})')
    return {'rodada': run_txt, 'espelho': base, 'vento': rotulo_raj, 'grade': {'la0': LA0, 'lo0': LO0, 'st': ST, 'nla': NLA, 'nlo': NLO},
            't3': t3, 'chuva3': grade, 'raio3': graio, 'raj3': graj, 'raio_ok': len(pr_ok) > 1, 'raio_metodo': 'chuva convectiva em 3 h (cp) × CAPE (mucape), ECMWF IFS', 'dias': dias, 'hora_ini': hora_ini, 'mun': mun}


# ---------------------------------------------------------------- INMET
ICONES = []

def _icone(v):
    ic = v.get('icone') or ''
    if ic and ic not in ICONES:
        ICONES.append(ic)
    return ICONES.index(ic) if ic else -1


def _periodo(v):
    """Resumo de um período do INMET (dia inteiro ou turno)."""
    o = {'resumo': v.get('resumo') or v.get('tempo') or '', 'ic': _icone(v),
         'tmax': v.get('temp_max'), 'tmin': v.get('temp_min'),
         'vento': ' '.join(x for x in [v.get('dir_vento') or '', v.get('int_vento') or ''] if x),
         'umid': [v.get('umidade_min'), v.get('umidade_max')]}
    return o


def _poligono(txt):
    """GeoJSON do aviso com coordenadas arredondadas (arquivo menor)."""
    try:
        g = json.loads(txt) if isinstance(txt, str) else txt
        def rd(c):
            return [rd(x) for x in c] if isinstance(c[0], list) else [round(c[0], 3), round(c[1], 3)]
        return {'type': g['type'], 'coordinates': rd(g['coordinates'])}
    except Exception:
        return None


def inmet():
    prev, ok = {}, 0
    for g, n, *_ in MUN:
        try:
            r = get(f'{INMET}/previsao/{g}', tent=3)
            j = r.json() if r is not None else None
            dd = (j or {}).get(str(g)) or (list(j.values())[0] if j else None)
            if not dd:
                continue
            prev[g] = {}
            for data, v in dd.items():
                iso = '-'.join(reversed(data.split('/')))
                if v.get('manha') or v.get('tarde'):
                    tur = {k: _periodo(v[k]) for k in ('manha', 'tarde', 'noite') if v.get(k)}
                    base = dict(tur.get('tarde') or tur.get('manha') or next(iter(tur.values())))
                    tx = [t['tmax'] for t in tur.values() if t['tmax'] is not None]
                    tn = [t['tmin'] for t in tur.values() if t['tmin'] is not None]
                    base['tmax'] = max(tx) if tx else None
                    base['tmin'] = min(tn) if tn else None
                    base['tur'] = tur
                else:
                    base = _periodo(v)
                prev[g][iso] = base
            ok += 1
        except Exception as e:
            log(f'  INMET {n}: {e}')
    avisos = None
    try:
        r = get(f'{INMET}/avisos/ativos', tent=3)
        if r is not None:
            j = r.json()
            arr = []
            for k, v in (j.items() if isinstance(j, dict) else [('hoje', j)]):
                if isinstance(v, list):
                    arr += [(k, a) for a in v]
            avisos = []
            for k, a in arr:
                geo = str(a.get('geocodes') or '')
                mun = [m[1] for m in MUN if str(m[0]) in geo]
                if not mun:
                    continue
                avisos.append({'desc': a.get('descricao') or 'Aviso', 'sev': a.get('severidade') or '', 'cor': a.get('aviso_cor') or '#FF9900',
                               'ini': a.get('inicio') or '', 'fim': a.get('fim') or '', 'fut': k != 'hoje', 'mun': mun,
                               'riscos': a.get('riscos') or '', 'instr': a.get('instrucoes') or '', 'poli': _poligono(a.get('poligono'))})
    except Exception as e:
        log(f'  INMET avisos: {e}')
    log(f'INMET: {ok}/{len(MUN)} municípios; avisos: {"—" if avisos is None else len(avisos)}')
    return prev, ok, avisos


def main():
    try:
        import faulthandler
        faulthandler.enable(open(LOG, 'a', encoding='utf-8'))   # registra travamento nativo no log
    except Exception:
        pass
    forcar = '--forcar' in sys.argv
    log('=== início ===')
    anterior = ler_anterior()
    try:
        mod = modelo(forcar, anterior); erro_mod = ''
    except Exception as e:
        log('ECMWF ERRO: ' + str(e)); log(traceback.format_exc().strip().splitlines()[-1])
        mod = (anterior or {}).get('modelo'); erro_mod = str(e)[:200]
    prev, ok, avisos = inmet()
    if not ok and anterior:
        prev, ok = anterior.get('inmet', {}), 0
        ICONES[:] = anterior.get('icones', [])
        avisos = avisos if avisos is not None else anterior.get('avisos')
    dados = {
        'v': 2,
        'gerado': dt.datetime.now(TZ).strftime('%Y-%m-%dT%H:%M'),
        'modelo': mod, 'erro_modelo': erro_mod,
        'inmet': {str(k): v for k, v in prev.items()}, 'inmetOk': ok, 'avisos': avisos, 'icones': ICONES,
        'mun_info': [{'g': g, 'n': n, 'la': la, 'lo': lo, 's': s} for g, n, la, lo, s in MUN],
        'atribuicao': 'Adapted from "ECMWF IFS 15-day Forecast Data" by ECMWF, licensed under CC BY 4.0, available at https://data.ecmwf.int/forecasts/',
    }
    tmp = SAIDA + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(PREFIXO + json.dumps(dados, ensure_ascii=False, separators=(',', ':')) + ';\n')
    os.replace(tmp, SAIDA)
    log(f'gravado previsao_tempo.js ({os.path.getsize(SAIDA) / 1e3:.0f} kB)')
    return 0 if (mod and not erro_mod) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        log('ERRO GERAL: ' + traceback.format_exc())
        sys.exit(2)
