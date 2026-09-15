// PM2, same shape as the other collectors on this machine.
//   pm2 start ecosystem.config.cjs
//   pm2 logs digger-weekly
//
// Friday 10:00, ahead of preparing the weekend's music, which is when his
// brief asks for it. autorestart is off: this is a cron job, not a daemon,
// and it is meant to exit.
module.exports = {
  apps: [
    {
      name: "digger-weekly",
      script: ".venv/Scripts/python.exe",
      args: "weekly.py",
      cwd: __dirname,
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 10 * * 5",
      out_file: "data/weekly.out.log",
      error_file: "data/weekly.err.log",
      time: true,
    },
  ],
};
