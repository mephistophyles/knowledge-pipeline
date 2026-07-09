# infra — AWS foundation (Terraform)

One `terraform apply` provisions the whole tier-1 foundation:

- **S3 bucket** — raw store + Litestream backups (versioned, encrypted, private, IA lifecycle)
- **IAM role + instance profile** — the box talks to S3 with *no keys on disk*
- **Security group** — SSH locked to your IP; 80/443 open for the dashboard
- **EC2 box** — Ubuntu + Docker, the always-on control box
- **Elastic IP** — stable address that survives restarts
- **Route 53 A record** — `pipeline.madebyphil.com` → the box

## One-time prerequisites

### 1. Install the tools (macOS)

```bash
brew install awscli terraform
```

### 2. Give the AWS CLI credentials

Long-lived root keys are dangerous — make a dedicated admin user instead:

1. AWS Console → **IAM** → **Users** → **Create user** (e.g. `phil-cli`).
2. Attach the **AdministratorAccess** policy (fine for a solo operator bootstrapping; we can tighten later).
3. Open the user → **Security credentials** → **Create access key** → "Command Line Interface".
4. Configure the CLI with it:

```bash
aws configure
#   AWS Access Key ID:     <paste>
#   AWS Secret Access Key: <paste>
#   Default region name:   us-east-1
#   Default output format:  json

aws sts get-caller-identity   # should print your account + user ARN
```

### 3. Make an SSH key for the box

```bash
ssh-keygen -t ed25519 -f ~/.ssh/knowledge-pipeline -C "knowledge-pipeline"
# creates ~/.ssh/knowledge-pipeline (private) and ~/.ssh/knowledge-pipeline.pub (public)
```

## Apply

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars          # set raw_bucket_name + my_ip_cidr (curl ifconfig.me)

terraform init                    # downloads the AWS provider
terraform plan                    # DRY RUN — read what it will create, nothing happens yet
terraform apply                   # type "yes" to build it
```

`terraform plan` is your safety net: it prints every resource before anything is
created and never changes your account. Read it, then `apply`.

After apply, Terraform prints the dashboard URL, the box IP, and an `ssh` command.

## Prerequisite check

- `madebyphil.com` must already be a **hosted zone in Route 53** (the DNS record
  step looks it up). Verify: `aws route53 list-hosted-zones --query "HostedZones[].Name"`.
  If it's not there, register/transfer the zone into Route 53 first.

## Teardown

```bash
terraform destroy    # removes everything this module made
```

(The S3 bucket must be emptied first if it has objects — Terraform will tell you.)

## Notes

- **State**: `terraform.tfstate` stays local and is gitignored (it can hold
  secrets). Fine for one operator; we can move it to an S3 backend later.
- **Cost**: ~$15–30/mo (instance + EBS + tiny S3). `terraform destroy` stops the
  meter completely.
