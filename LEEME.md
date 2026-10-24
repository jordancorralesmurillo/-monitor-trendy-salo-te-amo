# Monitor Trendy – Calendario de adviento Luxury

Revisa la tienda cada 10 minutos (en los servidores de GitHub, con tu PC apagado)
y te avisa por Telegram cuando aparece un producto con "luxury", "adviento" o "calendario".

## Pasos
1. En Telegram habla con @BotFather -> /newbot -> guarda el TOKEN.
   Escríbele un mensaje a tu bot y obtén tu CHAT ID con @userinfobot.
2. En github.com crea un repositorio PÚBLICO (New repository).
3. Sube TODO el contenido de esta carpeta (incluida la carpeta oculta .github).
   Si .github no se sube al arrastrar, créala con "Add file > Create new file"
   escribiendo la ruta  .github/workflows/monitor.yml  y pegando su contenido.
4. Settings > Secrets and variables > Actions > New repository secret:
     TELEGRAM_TOKEN    = tu token
     TELEGRAM_CHAT_ID  = tu chat id
5. Pestaña Actions: acepta habilitar los workflows.
6. Actions > "Monitor calendario de adviento" > Run workflow:
   - Primero con la casilla "probar" marcada: debe llegarte un mensaje a Telegram.
   - Luego sin marcarla: el log debe decir "Sin novedades" y sin errores.

## Ajustes (en monitor.py)
- PAGINAS: páginas que se vigilan.
- PALABRAS: palabras que activan el aviso.
- Intervalo: en .github/workflows/monitor.yml, línea cron ("*/10 * * * *" = cada 10 min).

## Para apagarlo
Pestaña Actions > el workflow > menú "..." > Disable workflow.
