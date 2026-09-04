class BudgetAgent:
    """
    Handles budget allocation and adjustments for ad campaigns.
    """

    def allocate_budget(self, total_budget, distribution_rules=None):
        """
        Distribute a total budget across multiple campaigns or ad sets.
        For now, this is just a placeholder example.
        """
        if distribution_rules is None:
            distribution_rules = {"default": 1.0}  # 100% to a single place

        # Example simple logic: all to one campaign
        allocated = {}
        for campaign_name, ratio in distribution_rules.items():
            allocated[campaign_name] = total_budget * ratio
        
        return allocated


if __name__ == "__main__":
    # Quick test
    agent = BudgetAgent()
    example_distribution = {"CampaignA": 0.6, "CampaignB": 0.4}
    result = agent.allocate_budget(100.0, example_distribution)
    print("Allocated Budget:", result)
