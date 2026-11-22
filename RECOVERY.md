# Recovering from the cost guardrail

The guardrail (`infra/main/budget.tf`) acts at three levels of actual monthly
spend against the `pidge-monthly` budget (default $5; expected spend is about
$0.30):

| Spend | What happens | Undo |
|---|---|---|
| 50% actual, 100% forecast | email to your address (`OWNER_EMAIL`) | nothing to undo |
| 100% actual | **kill switch**: the function deactivates the SES receipt rule set and sets its own reserved concurrency to 0 | steps 2 and 3 |
| 200% actual | **budget action**: `budget-deny-all` (Deny `*` on `*`) is attached to `pidge-lambda` and `pidge-deploy` | step 1 |

Recover with root or other administrator credentials. The deploy role can't
do it: once the deny is attached, it can do nothing at all. Find out why spend
went up before you undo anything.

The examples use the AWS CLI in us-west-2:

```sh
export AWS_REGION=us-west-2
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

## 0. See what fired

```sh
aws budgets describe-budget-actions-for-budget \
  --account-id "$ACCOUNT_ID" --budget-name pidge-monthly \
  --query 'Actions[].{id: ActionId, status: Status}'
aws iam list-attached-role-policies --role-name pidge-deploy
aws iam list-attached-role-policies --role-name pidge-lambda
aws lambda get-function-concurrency --function-name pidge
aws ses describe-active-receipt-rule-set
```

- If `budget-deny-all` is attached to either role, the 200% action fired.
- If the reserved concurrency is `0`, or `describe-active-receipt-rule-set`
  prints nothing, the kill switch fired.

Do the steps in this order.

## 1. Reverse the budget action (200%)

```sh
ACTION_ID=$(aws budgets describe-budget-actions-for-budget \
  --account-id "$ACCOUNT_ID" --budget-name pidge-monthly \
  --query 'Actions[0].ActionId' --output text)
aws budgets execute-budget-action \
  --account-id "$ACCOUNT_ID" --budget-name pidge-monthly \
  --action-id "$ACTION_ID" --execution-type REVERSE_BUDGET_ACTION
```

Check that the policy is gone from both roles (`list-attached-role-policies`
above). If the reversal fails, detach it directly:

```sh
for role in pidge-deploy pidge-lambda; do
  aws iam detach-role-policy --role-name "$role" \
    --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/budget-deny-all"
done
```

Console: Billing and Cost Management, Budgets, `pidge-monthly`, Actions, select
the action, **Reverse action**.

## 2. Let the function run again (100%)

```sh
aws lambda delete-function-concurrency --function-name pidge
```

Console: Lambda, `pidge`, Configuration, Concurrency, Edit, **Use unreserved
account concurrency**.

Do this before any deploy. The deploy workflow refuses to apply while the
reserved concurrency is 0, so a routine push can't undo the kill switch by
accident.

## 3. Turn SES receiving back on (100%)

```sh
aws ses set-active-receipt-rule-set --rule-set-name pidge
aws ses describe-active-receipt-rule-set --query Metadata.Name
```

Console: Amazon SES, Configuration, Email receiving, rule set `pidge`, **Set as
active**.

## 4. Afterwards

- To raise the limit, run
  `gh variable set BUDGET_LIMIT_USD --repo moshez/pidge --body 10`, then deploy
  (`gh workflow run deploy.yml --repo moshez/pidge --ref trunk`). A spend that
  is still over the threshold can set off the guardrail again.
- Some mail may have reached the bucket while the function was stopped. The
  reconciler's inbound sweep picks it up within 15 minutes of step 2.
- To check everything end to end, run `python3 infra/setup.py` again with the
  same arguments. Every step it has already done is a no-op.
