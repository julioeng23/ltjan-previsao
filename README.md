# Previsão do tempo — LT JAN

Gera a cada 3 h o arquivo `previsao_tempo.js` lido pelo *Diagrama de Avanço LT JAN.html*
(chuva, rajadas e índice de raios do ECMWF IFS Open Data; municípios e avisos do INMET).

- O GitHub Actions roda `previsao_tempo.py` e publica o resultado na branch `dados`, sempre
  substituindo a versão anterior (um único commit, repositório não cresce).
- O GitHub Pages serve a branch `dados`: `https://<usuário>.github.io/<repositório>/previsao_tempo.js`.
- Para forçar uma atualização: aba **Actions → Previsao do tempo LT JAN → Run workflow**.

Fonte do modelo: Adapted from "ECMWF IFS 15-day Forecast Data" by ECMWF, licensed under CC BY 4.0,
available at https://data.ecmwf.int/forecasts/. Dados municipais e avisos: INMET.
