import os
import re
from pathlib import Path

def get_launcher_version(environment="LIVE"):
    """
    Extrai a última versão conhecida do Star Citizen para um ambiente específico
    lendo o arquivo de log do próprio RSI Launcher.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("Erro: Variável de ambiente APPDATA não encontrada.")
        return None
        
    log_path = Path(appdata) / "rsilauncher" / "logs" / "log.log"
    if not log_path.exists():
        print(f"Erro: Arquivo de log do launcher não encontrado em {log_path}")
        return None
        
    # O launcher registra linhas como:
    # [Pipeline] Verifying Star Citizen LIVE 4.8.1-live.11952564 at E:\...
    # [Pipeline] Installing Star Citizen HOTFIX 4.8.1-live.12015818 at E:\...
    
    # regex para capturar a versão (ex: 4.8.1-live.11952564)
    # Procuramos pela palavra chave (Installing ou Verifying), depois Star Citizen, 
    # depois o ambiente, e então a string de versão.
    if environment.upper() == "ANY":
        env_pattern = r"\w+"
    else:
        env_pattern = environment.upper()
        
    pattern = re.compile(rf"\[Pipeline\] (?:Verifying|Installing) Star Citizen {env_pattern} ([\w\.-]+) at")
    
    last_version = None
    
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    last_version = match.group(1)
    except Exception as e:
        print(f"Erro ao ler o arquivo de log: {e}")
        return None
        
    return last_version

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pega a versão do jogo pelo log do RSI Launcher.")
    parser.add_argument("--env", default="LIVE", help="Ambiente (LIVE, PTU, HOTFIX, etc)")
    parser.add_argument("--raw", action="store_true", help="Saída crua apenas com o display version (ex: 4.8.1)")
    args = parser.parse_args()
    
    version = get_launcher_version(args.env)
    if version:
        parts = version.split('-')
        display_version = parts[0] if len(parts) >= 1 else version
        
        if args.raw:
            print(display_version)
        else:
            print(f"Versão mais recente detectada para {args.env}: {version}")
            if len(parts) >= 2:
                # Extraindo p4cl (os ultimos digitos apos o ponto)
                p4cl = parts[-1].split('.')[-1]
                lce_format = f"{display_version}-{args.env.upper()}-{p4cl}"
                print(f"Formato LCE: {lce_format}")
    else:
        if not args.raw:
            print(f"Não foi possível encontrar a versão para o ambiente {args.env} nos logs do launcher.")
