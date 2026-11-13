using System.Windows;
using System.Windows.Controls.Primitives;

namespace NewsStudio.App.Controls;

public sealed class ResponsiveUniformGrid : UniformGrid
{
    public double Breakpoint { get; set; } = 690;
    public int NarrowColumns { get; set; } = 2;
    public int WideColumns { get; set; } = 3;

    protected override Size MeasureOverride(Size constraint)
    {
        var availableWidth = double.IsInfinity(constraint.Width) ? ActualWidth : constraint.Width;
        var desiredColumns = availableWidth > 0 && availableWidth < Breakpoint ? NarrowColumns : WideColumns;
        if (Columns != desiredColumns) Columns = desiredColumns;
        return base.MeasureOverride(constraint);
    }
}
