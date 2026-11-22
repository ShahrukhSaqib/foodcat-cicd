# Food Cat: a real CI/CD pipeline with GitHub Actions and AWS

Name: Shahrukh Saqib
ERP: 22645

## What this repository shows

Food Cat is the food delivery app from my architecture assignment (a gateway, four services, three queues, a cache and a web page). For this assignment it got a real pipeline. Every push to the main branch now travels the whole way on its own:

    code change → git push → unit tests and security scans → build zip → Amazon S3 → EC2 server → check of the live site

In other words, nobody copies files to the server by hand any more, and a change that breaks a test never gets anywhere near production. The whole pipeline is one file, `.github/workflows/cicd.yml`. The rest of this page explains each step, the AWS side, the security checks (the bonus part) and, further down, the app itself.

## The pipeline, step by step

The job names in GitHub Actions carry the step numbers of the assignment, so the run graph reads like the brief.

* Steps 1 to 3 (change, push, trigger): the workflow starts on every push to `main`. It also has a 'Run workflow' button, which is handy for a rehearsal. Only one run can deploy at a time, and a newer push waits for the older one instead of cutting it off halfway.
* Step 4 (unit tests): `python -m pytest` runs the 187 tests on Python 3.12. If a single test fails, the job turns red and every job after it is skipped. Hence nothing is built, stored or deployed.
* Bonus (security scans): Bandit, pip-audit and Gitleaks run next to the tests, and the build waits for both jobs. The bonus section below explains them.
* Step 5 (build): `deploy/build.py` compiles every Python file and packs the app into `build-v<VERSION>-<run number>.zip`, for example `build-v1.1-7.zip`. The zip holds only what the server needs (the code, the web page, the launcher, `requirements.txt` and the deploy scripts) plus a small `build-info.json` with the version, the run number and the commit. Tests and local data stay out. The same zip is also kept as an artifact of the run in GitHub.
* Step 6 (store in S3): the zip goes to `s3://<bucket>/builds/`. Only after that upload has worked does the job write `builds/latest.txt`, a one line file that names the newest zip. So the pointer always names the latest build that passed everything before it.
* Step 7 (deploy): GitHub Actions never logs in to the server. Instead it asks AWS Systems Manager ('Run Command') to run `deploy/pull_latest_build.sh` on the EC2 instance. That script reads `latest.txt`, downloads the zip from S3 with the server's own IAM role and hands over to `deploy/install_build.sh`. That one installs the build in a new folder with its own Python packages, points `/opt/foodcat/current` at it, restarts the `foodcat` service and waits until the new build answers. If it doesn't answer within a minute, the server goes back to the build that ran before, and the job turns red.
* Step 8 (verify): the last job asks the public address of the server which build it runs (`/api/version`). It has to be the build of this very run, and the home page has to load as well. The job summary then shows the link to the live site, and the run page shows the same link next to the word 'production'.

The version is printed at the bottom of every page and under the login form too, for example 'Version 1.1, build 7 (commit a1b2c3d)'. Therefore the change is visible to a person, not only to the pipeline.

## How a change goes live (what my video shows)

1. Change something visible. For example, raise `VERSION` from 1.0 to 1.1 and change the headline in `web/app.js` (search for 'Hungry?').
2. Commit and push: `git add .`, then `git commit -m "Version 1.1: new headline"`, then `git push`.
3. The run starts by itself in the Actions tab: tests and scans, then the build, S3, the deploy and the check.
4. The S3 bucket now holds `build-v1.1-<run number>.zip`, and `builds/latest.txt` names it.
5. The live site (the link on the run page) shows the new headline and 'Version 1.1' after a refresh.

## The AWS side

Everything lives in one region.

* S3 bucket `foodcat-builds-22645`. It is private ('Block all public access' is on) and only stores the build zips and `latest.txt`.
* EC2 instance `foodcat-prod`: Amazon Linux 2023 on a t3.micro. Its security group lets in HTTP on port 80 and nothing else. There is no SSH port and no key pair, because Systems Manager does the deploys (and Session Manager can open a shell in the browser if I ever need one).
* IAM role `FoodCatEC2Role` for the instance. It has `AmazonSSMManagedInstanceCore`, so that Systems Manager can reach the server, and one small policy that lets it read `builds/*` from the bucket. Nothing more.
* IAM user `github-foodcat` for the pipeline. Its policy allows four things only: putting zips into `builds/` (and listing the bucket), sending the `AWS-RunShellScript` command to this one instance, reading the result of that command, and looking up the instance's public address. Its access key is the only secret in GitHub.
* GitHub settings (Settings, Secrets and variables, Actions): the secrets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, and the variables `AWS_REGION`, `S3_BUCKET` and `EC2_INSTANCE_ID`. The region is a variable on purpose. GitHub hides every secret value in the logs, and since the region is part of the server's address, a secret region would hide the link to the live site as well.

The server needed no setup by hand. The first deploy installs Python 3.12, creates an ordinary user called `foodcat`, makes a random `SECRET_KEY` (in `/etc/foodcat/foodcat.env`, which only root can read) and installs the systemd service from `deploy/foodcat.service`. Later deploys skip all of that. On the server itself:

* `/opt/foodcat/releases/` keeps the three newest builds, each with its own virtual environment, and `/opt/foodcat/current` points at the live one. Hence a rollback is one link and one restart.
* `/var/lib/foodcat` holds the databases, outside every build, so a deploy never wipes the accounts or the orders.
* systemd starts `run_local.py --host 0.0.0.0 --port 80 --data-dir /var/lib/foodcat` as the `foodcat` user. Only the gateway listens on port 80, while the six programs behind it stay on 127.0.0.1. The service may open port 80 (`CAP_NET_BIND_SERVICE`) and has no other special rights.

## Security in the pipeline (the bonus)

The job 'Bonus: Security scans (DevSecOps)' runs three checks side by side with the unit tests, and the build waits for it. Hence a finding blocks the deploy in exactly the same way as a failing test, which is the 'shift left' idea from class.

* SAST with Bandit: it reads the Python code and looks for risky patterns, such as shell commands built from input, `debug=True` or weak hashing. Medium and high findings stop the pipeline.
* SCA with pip-audit: it checks every package in `requirements.txt` (and the packages those pull in) against the Python Packaging Advisory Database. A package with a known vulnerability stops the pipeline.
* Secret scanning with Gitleaks: it reads every commit in the history, not only the latest files, and stops the pipeline if anything looks like a password or a key. The workflow downloads Gitleaks 8.30.1 itself and compares the file with its published SHA 256 checksum before running it.

A few smaller choices belong to the same idea.

* Every action in the workflow is pinned to an exact commit, with its version in a comment. A tag can be moved to different code later, but a commit cannot (this really happened to a popular scanner action in March 2026).
* The workflow may only read the repository (`permissions: contents: read`), and the checkout step doesn't keep its token.
* The pipeline's AWS user has the smallest permissions that still work, the server has no SSH port, and the app runs as an ordinary user.
* Dependabot (`.github/dependabot.yml`) opens a pull request when a package, the Docker base image or one of the actions has a newer version.

Two additions would come next. First, GitHub OIDC instead of the access key, so that the pipeline gets a short lived role and no key lives in GitHub at all. Second, a DAST scan with OWASP ZAP against the live site after step 8.

## Run it on your computer

You need Python 3.10 or newer, because the packages in `requirements.txt` need it. I wrote and tested everything on 3.13, and the server and the pipeline use 3.12.

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python run_local.py

On Mac or Linux the second line is `source .venv/bin/activate`. If PowerShell refuses to run the activation script, use Command Prompt instead. Then open http://127.0.0.1:8000 in a browser. You do not need a `.env` file for this run. When there is no usable `SECRET_KEY`, the launcher invents a random one, which means that everybody has to log in again after a restart. The launcher starts seven small programs (the gateway, the user, payment, delivery and alert services, and two copies of the order service) and stops them all when you press Ctrl+C. Add `--fresh` to wipe the old data first. The bottom of the page says 'Version 1.0 (local copy)', because only the pipeline gives a build its number.

`python deploy/build.py` makes the same kind of zip as step 5, in the `dist` folder, if you'd like to look inside one.

For a guided tour, open a second terminal in the same folder and run:

    python demo.py

It plays one customer and two riders through the whole system and explains each step. The two riders go offline again when it ends, so you can run it again straight away (the login limit that 'If something does not work' describes only matters if you play it more than three times in one minute). Add `--break-cache` to also watch the menu survive a dead cache (this one needs the local run, because it talks to the order services directly). `python demo.py --base http://<the server's address>` plays the same order through the live site on AWS.

To use the web page yourself, sign up once as a customer and once as a rider (use two browser tabs, because each tab keeps its own login). The rider should go online first. Otherwise the paid order just waits in its queue until somebody does, which is a nice thing to show in a viva, by the way. After 'Place order' the page jumps to 'My Orders', where a coloured banner follows the order (yellow while it is on its way, green once it is confirmed or delivered, red if the card is declined). The 'System' tab is a live copy of my drawing: green dots for services that answer, and numbers for the messages waiting in each queue.

## Run it with Docker

    copy .env.example .env
    python -c "import secrets; print(secrets.token_hex(32))"

On Mac or Linux use `cp` instead of `copy`. Paste the long random text from the second command into `.env` as the value of `SECRET_KEY`. Then:

    docker compose up --build

and open http://127.0.0.1:8000 again. `docker compose down --volumes` stops everything and deletes the data. Each service gets its own container and its own address inside the network, and each one gets its own database volume too (the two order instances share one, because they are two copies of the same service). Only the gateway is opened to your computer, which means that the payment service has no public door at all. The AWS server doesn't use Docker. It runs the build zip directly, which is what the assignment asks for.

Two experiments are worth showing in a viva. The tests check the same two behaviours without containers, but I have not tried them with Docker itself yet. First, `docker compose stop payment` and then place an order. The order is still accepted at once, the System tab shows the new orders queue growing, and nothing is lost. After `docker compose start payment` the queue drains and the alerts arrive. Second, `docker compose stop order-1` and keep browsing the menu. The gateway quietly sends every call to order-2.

## If something does not work

The pipeline:

* Step 6 fails with 'Set the repository variable': a variable is missing or misspelt in Settings, Secrets and variables, Actions, Variables. All three names are in capitals.
* Step 6 fails with 'Access Denied': the bucket name in the IAM policy of `github-foodcat` and the `S3_BUCKET` variable are not the same, or the access key belongs to a different user.
* Step 7 says that Systems Manager cannot reach the instance: the instance is stopped, it lives in a different region than `AWS_REGION`, or it lacks the `FoodCatEC2Role` role. Systems Manager, Fleet Manager has to show it as 'Online'. A role that was attached after the launch can take a few minutes to count.
* Step 7 fails on the server: the job prints everything the server printed. The usual causes are a mistake in the IAM policy of the role (the server can't read the zip) or no internet access for the server (it downloads Python and the packages on the first deploy).
* Step 8 can't reach the site: the security group of the instance needs a rule for HTTP on port 80 from anywhere.
* The browser still shows the old version: refresh the page (Ctrl+F5). After a stop and start, the instance also gets a new public address. The pipeline looks it up on every run, so simply follow the link on the newest run page.

The app on your computer:

* The launcher says that a service stopped while starting, or did not start in time: another program is probably using one of the ports (8000, 5001 to 5005 and 5012). A common culprit is a Food Cat that is still running from before, for example in a terminal that was closed without pressing Ctrl+C. End the leftover python programs (in Task Manager on Windows) and try again. The same goes for running the Docker version and `python run_local.py` together, because both want port 8000.
* `python demo.py` says that the job went to a rider who was already online: somebody from the web page is still online and is closer than Ali. Stop the launcher and start it again with `--fresh`.
* The web page or `python demo.py` says 'Too many login attempts. Please wait a minute.': the gateway allows 10 login attempts in any minute from one computer, and a correct login counts as well. The demo alone uses three. Hence, playing it several times in a row and then clicking through the page can reach the limit, and the cure is simply to wait a minute.
* Docker complains about `SECRET_KEY`, or the gateway and user containers exit straight away (`docker compose logs gateway` shows why): the `.env` file still has the example value, or an empty one. Put the long random text in it.
* `python demo.py` says that it could not reach the gateway: start `python run_local.py` first, in another terminal.
* You unzipped this version on top of an older copy and the old `data` folder is still there: nothing to do, because the Order Service adds the missing cuisine to the old menu table by itself. Start once with `--fresh` if you would rather begin from zero.
* The computer uses a proxy: that is fine, because Food Cat talks to its own programs directly and ignores the proxy.

## My diagram, box by box

* API Gateway and Load Balancer: `foodcat/gateway.py`. It checks the login token, replaces it with two trusted headers, hands calls to the two order instances in turn (and moves on to the other if one is dead), limits login attempts, serves the web page, and produces the monitoring report. It also answers `/api/version`, which says which build is live.
* User Service: `foodcat/user_service.py`. Sign up, login, and 'who am I'.
* Order Service: `foodcat/order_service.py`. Menus come from the cache, orders go to the Order DB, and a message goes to the 'new orders' queue.
* Cache: `foodcat/cache.py`. Popular menus for 30 seconds. If it dies, the Order Service reads the database instead.
* Payment Service: `foodcat/payment_service.py`. Listens to 'new orders', charges the card, and posts to 'paid orders'.
* Delivery Service: `foodcat/delivery_service.py`. Listens to 'paid orders' and gives each order to the nearest free rider. It also has the small API that the rider uses.
* Lambda and SNS: `foodcat/alert_lambda.py`. Reads 'updates' and sends push, SMS and email alerts.
* The three queues: `foodcat/sqs.py`.
* Reports (the OLAP cubes): `foodcat/reports.py`. Run it from the project folder with `python -m foodcat.reports`, after the demo has made a few paid orders. It is a batch job, which is why it has no arrows in my drawing. It reads the `data` folder of the local run, so it is not part of the Docker setup, where every service keeps its data in its own volume.
* Docker containers: `Dockerfile` and `docker-compose.yml`.
* CI/CD: `.github/workflows/cicd.yml`, with the scripts it uses in `deploy/`.
* SAST: Bandit, in the security job of the same pipeline. My drawing says SonarQube. Bandit does the same job for Python without a server or an account, and SonarCloud could take its place later.
* Vault: `.env.example` on your computer and `/etc/foodcat/foodcat.env` on the server. The key that signs the tokens is read from the environment, never from the code.
* Monitoring and alerts: the `/api/monitor` report and the System tab.
* The web page, which is my client tier: `web/`. Its look comes from the logo, which has three colours (midnight navy, tangerine orange and golden amber). `web/style.css` keeps them as named values at the top, so a different brand would be a few lines. The search box and the cuisine buttons work inside the browser, on the menu that the Order Service sent.
* Kubernetes is the one thing I left out. The labs stopped at Docker Compose, so I did too.

## How one order travels

1. The customer clicks 'Place order'. The gateway checks the token and passes the call to one of the two order instances.
2. The Order Service ignores whatever prices the browser sent and works out the total from its own database. For example, two Zinger Burgers and a Cold Coffee always come to Rs 1,380. It saves the order, puts a message on 'new orders', and answers in a few milliseconds. It never waits for the payment.
3. The Payment Service picks the message up and charges the (pretend) card. If it works, the order moves on to 'paid orders'. If the card is declined, the customer is told and nobody else hears about it.
4. The Delivery Service takes the paid order and looks at every rider who is online and free. The nearest one wins, and in my demo that is Ali, who is 3.7 km from the restaurant.
5. Each step also drops a short fact on the 'updates' queue ('payment received', 'rider assigned'). The Lambda function turns these facts into sentences and sends them by push, SMS and email.
6. The rider taps 'Picked up' and then 'Delivered' on the page. Both taps create more updates, and the rider becomes free for the next order.

So what happens when something breaks? Quite a lot has been prepared for, and the tests check every item on this list.

* The payment company does not answer: the message goes back on the queue and is tried again. After five failures it moves to the dead letter queue, which the System tab turns red.
* No rider is free: the paid order simply waits in its queue and is tried again every few seconds.
* The same message arrives twice (queues are allowed to do that): the card is charged once, one rider is chosen, and the customer gets one alert, not two.
* The service crashes right after charging the card: the retry gets the old answer from the card company, because the order number is used as an 'idempotency key'. Nobody pays twice.
* The cache dies: the menu comes from the database. Slower, but nobody notices.
* One order instance dies: the gateway tries the next one. A write is only repeated when the first attempt never even reached the service (a refused connection, or a name that Docker no longer knows), so an order cannot be created twice by accident.
* Everything starts at the same moment, which is what Docker does: the programs take turns on a brand new database file instead of crashing into each other, and the second order instance does not add the menu a second time.
* A new build does not start on the server: the deploy script puts the previous build back within about a minute, and the pipeline turns red.

## What is real and what is pretend

Real: seven separate programs (HTTP at the front door, queues behind it), one database per service, signed login tokens, salted password hashing, the whole message flow, the retry logic, the load balancing, the web page, and now the pipeline with a real S3 bucket and a real EC2 server.

Pretend:

* SQS is one SQLite file with the same rules (send, receive, hide for a while, delete). On AWS this file would be replaced by calls to the real service.
* ElastiCache is a dictionary with an expiry time. Each order instance has its own, so a real shared cache such as Redis would hit the database less often.
* The card company is a few lines of code, and the card token decides the result (`tok_visa` works, `tok_decline` is refused, `tok_error` is a company that does not answer).
* SNS writes rows into a table. A real phone would be pushed the message, so the web page asks for new alerts every three seconds instead.
* Lambda is an ordinary Python function with the same shape as a real handler. A small loop plays the part of the event source.
* The NoSQL rider database is a table of JSON documents inside SQLite.
* Vault is an environment variable (on the server, a file that only root can read).
* The OLAP cube is a batch script and one table.

Left out on purpose: the restaurant portal, HTTPS, Kubernetes and real money. The live site is plain HTTP, which is fine for a class demo but not for real customers.

One limit that I only noticed late. The Order DB never learns what happened to an order after it was saved. The web page works out the stage ('Confirmed', 'Rider assigned', 'Delivered') from the alerts instead. In a bigger design I would let the Order Service listen to the updates through a queue of its own.

## Why these tools

* Python and Flask, because they are small enough to read in one sitting.
* SQLite, because every service needs a database of its own and I did not want anybody to install five database servers. While customers are being served, each service opens only its own file. The report job is the one program that reads more than one, which is fine for a batch job.
* Plain HTTP between the gateway and the services, and queues between the services. This is the same split as in my drawing: the solid arrows are calls that need an answer now, and the queues carry work that is allowed to wait.
* GitHub Actions, because the code already lives on GitHub and the course uses it. S3 keeps every build, so an old version can always be fetched again.
* Systems Manager instead of SSH for the deploy. With SSH the server would need port 22 open to the whole internet (GitHub's runners have no fixed address) and a private key would have to live in GitHub. With Systems Manager the server only needs an IAM role, and the pipeline only needs permission to send one kind of command to one instance. AWS CodeDeploy would also work, but it needs an agent, an application, a deployment group and an `appspec.yml`, which is a lot of setup for one small server.
* A zip and a virtual environment on the server, instead of Docker images, because the assignment asks for a build artifact in S3 that the server deploys. A t3.micro also has only 1 GB of memory, which is plenty for seven small Python programs but tight for building images.

## Security choices in the app

* There is no secret in the code. The gateway and the user service refuse to start without a proper `SECRET_KEY`, the file `.env` is ignored by git, and the example value is rejected on purpose. On the server the key is made by the first deploy and never leaves it.
* Passwords are salted and hashed with PBKDF2 (600,000 rounds, which is the figure OWASP gives for it today). PBKDF2 ships with Python, so there is no extra package to install. OWASP actually prefers Argon2id, and in a real product I would switch to that. A wrong email and a wrong password give the same answer, and login attempts are limited to 10 a minute for each client (a correct login counts too, which keeps the rule simple).
* The gateway is the only door. It removes any identity headers that a client sends and sets its own after checking the token. Odd URLs, such as `..` tricks or the `admin` switch for the cache, are refused, and the payment service is not on the list of places it will forward to.
* The server decides the prices. Quantities have to be 1 to 20, an address can have at most 200 characters, and requests bigger than 64 KB are refused.
* The web page never uses `innerHTML` (a test fails if anybody adds it). Everything the server sends goes in as plain text, so an address like `<b>Phase 6</b>` stays plain text. Moreover, the gateway sends a content security policy, which tells the browser to run only scripts that come from this site.
* The containers run as an ordinary user with every Linux capability dropped, and on the EC2 server the app runs as the ordinary `foodcat` user, which cannot change its own code.
* Logs name the event but never print an address or a card token.

## What could be better on AWS

The deploy part of my old 'Moving to AWS' plan is now real. The stand ins are still stand ins, though, and this is how each of them would move. Each pretend part lives in one small place and every setting comes from an environment variable, so most of the move would be a swap of those small places and not a rewrite of the services.

* Queues: `foodcat/sqs.py` is the only file that knows how the queues are stored. Its four calls (`send`, `receive`, `delete` and `size`) match what Amazon SQS offers, and the rule that hides a message for a while is the SQS visibility timeout. The dead letter rule (five failed tries) is code in `consume()` today. On AWS it becomes a setting of the queue itself, a redrive policy with a maximum receive count of five.
* Cache: `foodcat/cache.py` becomes Amazon ElastiCache for Redis. Both order instances would share it, which removes the limit that I wrote about under 'What is real and what is pretend'.
* Databases: the SQLite files become Amazon RDS (users, orders and payments) and Amazon DynamoDB (the rider documents). This is the biggest job, because the SQL is written for SQLite (for example the `?` placeholders), so a few queries would need small changes. Most services open their database through `db()` in `foodcat/common.py`, which helps.
* Alerts: `foodcat/alert_lambda.py` already has the shape of a Lambda function, `handler(event)`, and its event looks like what SQS hands to Lambda. On AWS the 'updates' queue would be the event source, and Amazon SNS would send the push, SMS and email instead of writing rows into the `outbox` table.
* Card company: the pretend provider becomes the test mode of a real one. The idempotency key is already sent with every charge.
* Secrets: `SECRET_KEY` would move from the file on the server to AWS Secrets Manager (or Vault, as in my drawing). The services only ever read an environment variable, so they do not care where the value comes from.
* Load balancing and HTTPS: an Application Load Balancer in front of two or more servers, with a certificate from AWS Certificate Manager. The `/health` page of every service is already there for its checks.
* Monitoring: every service prints one line per event, and systemd already collects them (`journalctl -u foodcat`). The CloudWatch agent could send them to CloudWatch Logs, and an alarm on the dead letter queue would replace the red bubble on the System tab.
* One change before real traffic: the Flask server that ships with Flask is fine for a class demo, but each service would run under a production server such as gunicorn.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest

There are 187 tests and they take about fifteen seconds. They cover the building blocks, each service on its own, the gateway, whole journeys from sign up to the doorstep, the demo script, and some timing traps (for example, every program starting in the same moment). `tests/test_release.py` covers the pipeline's own pieces: the version that the live site shows, what goes into the build zip (and what stays out), code that doesn't compile never reaching S3, the launcher settings of the server, and the deploy scripts. For the app itself, the code was broken on purpose in 40 places (for example: no login check, charging a card twice, choosing the farthest rider, trusting the price from the browser), and every one of those breaks made at least one test fail.

## Status of each part

* Every run of the pipeline is in the Actions tab of this repository, with the log of every step, and the S3 bucket keeps every build it made.
* Before the first push, steps 5 to 8 were rehearsed on one Linux computer with the workflow's own shell commands. A local stand in played S3, and the computer itself played the EC2 server, with small stand ins for systemd and dnf. Four builds went through: a first deploy on an empty server, a new version with a new headline (accounts made on the old build could still log in afterwards), a broken build that the server rolled back by itself, and a good build after that. A real browser then showed the new headline and the version line, with no errors.
* The workflow passes the `actionlint` checker, the deploy scripts pass `shellcheck`, and Bandit, pip-audit and Gitleaks found nothing.
* All 187 tests pass. The Python application was also run on Python 3.13 on one computer, where the launcher started all seven programs and the demo played through from sign up to delivery.
* The Compose file passed `docker compose config`. The Dockerfile build and the containers themselves have not been run yet, and they are not part of this pipeline.

## What is in the folder

    .github/workflows/cicd.yml   the CI/CD pipeline (steps 4 to 8 and the security scans)
    deploy/                      build.py (step 5), pull_latest_build.sh and install_build.sh (step 7), foodcat.service
    VERSION                      the version number that the pipeline and the page show
    foodcat/                     the services, the queue, the cache, the reports script
    web/                         the web page (index.html, app.js, style.css) and the logo pictures
    tests/                       the automated tests
    run_local.py                 starts everything without Docker (the server uses it too)
    demo.py                      the guided demo
    Dockerfile                   one recipe, one image per service
    docker-compose.yml           the whole system in containers
    .github/dependabot.yml, .env.example, requirements.txt, requirements-dev.txt
